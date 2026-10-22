"""Persistence and provenance regression tests; real fitting is tested separately."""

import hashlib
import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from calibration_lib.workbench.server import make_handler
from calibration_lib.workbench.service import Workbench


def put(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path):
    params = {
        "G0": 0.012,
        "k": 0.0184,
        "V0": 24.11,
        "b": 54.31,
        "c": 23459.83,
        "G0_err": 0.001,
        "k_err": 0.001,
        "V0_err": 0.01,
        "b_err": 0.1,
        "c_err": 1.0,
        "chisquare": 1.0,
    }
    put(tmp_path / "products/14B/tb/L2/json/2601010000_14B_TB.l2.json", [params] * 4)
    records = [
        {
            "channel": ch,
            "center": 300.0 + ch,
            "center_err": 1.0,
            "sigma": 10.0,
            "sigma_err": 1.0,
            "amplitude": 500.0,
            "amplitude_err": 1.0,
            "fwhm": 23.55,
            "resolution": 0.08,
            "resolution_err": 0.01,
            "temp": 20.0,
            "temp_err": 0.1,
            "bias": 28.5,
            "bias_err": 0.01,
            "window_lo": 250.0,
            "window_hi": 350.0,
            "fit_mode": "gaus",
            "rsquared": 0.99,
        }
        for ch in range(4)
    ]
    put(tmp_path / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json", records)
    return tmp_path


@pytest.fixture
def workbench(workspace):
    service = Workbench(workspace)
    yield service
    service.close()


def settled(service, run_id, job):
    # Windows file scanning and concurrent real demo jobs can delay the worker.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = service.job(run_id, job["id"])
        if result["status"] not in ("queued", "running"):
            return result
        time.sleep(0.02)
    pytest.fail("Background job did not finish within 30 seconds")


def test_history_is_input_not_a_successful_current_computation(workbench):
    run = workbench.create("14B")
    assert run["stages"]["TB_L1"]["history_count"] == 1
    assert run["stages"]["TB_L2"]["status"] == "not_run"
    assert run["stages"]["TB_L2"]["current_count"] == 0
    assert run["tb_version"]["origin"] == "historical"
    assert run["artifacts"] and all(a["source"] == "historical" for a in run["artifacts"])


def test_revisions_restore_after_restart_without_changing_history(workspace):
    before = {
        p: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (workspace / "products").rglob("*")
        if p.is_file()
    }
    service = Workbench(workspace)
    try:
        run = service.create("14B", "review")
        updated = service.patch(
            run["id"],
            {
                "windows": {"20C_285_001": {"2": {"lo": 255, "hi": 345}}},
                "excluded": ["14B:20C_285_001:1"],
                "reason": "review peak shoulder",
            },
        )
        assert updated["config_revision"] == 1
        assert "TB_L2" in updated["dirty_layers"]
        assert (service.root(run["id"]) / "revisions/0001.json").is_file()
    finally:
        service.close()
    reopened = Workbench(workspace)
    try:
        restored = reopened.view(run["id"])
        assert restored["config"] == updated["config"]
        assert restored["revisions"][-1]["reason"] == "review peak shoulder"
        assert restored["stages"]["TB_L2"]["status"] == "not_run"
    finally:
        reopened.close()
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}


@pytest.mark.parametrize(
    "patch",
    [
        {"windows": {"missing": {"0": {"lo": 200, "hi": 300}}}},
        {"windows": {"20C_285_001": {"4": {"lo": 200, "hi": 300}}}},
        {"windows": {"20C_285_001": {"0": {"lo": 300, "hi": 301}}}},
        {"windows": {"20C_285_001": {"0": {"lo": float("nan"), "hi": 350}}}},
        {"excluded": ["14B:unknown:0"]},
        {"products": "../products"},
    ],
)
def test_invalid_revision_does_not_mutate_durable_state(workbench, patch):
    run = workbench.create("14B")
    state_path = workbench.root(run["id"]) / "state.json"
    before = state_path.read_bytes()
    with pytest.raises((ValueError, KeyError, TypeError)):
        workbench.patch(run["id"], patch)
    assert state_path.read_bytes() == before


@pytest.mark.parametrize("run_id", ["../products", "..", "x/y", "x\\y", "", "/tmp/run"])
def test_run_path_rejects_traversal(workbench, run_id):
    with pytest.raises(ValueError):
        workbench.root(run_id)


def test_computations_cannot_write_to_historical_products(workbench, workspace):
    with pytest.raises(ValueError, match="runs"):
        workbench.adapter.execute("14B", "TB_L0", workspace / "products", {})


def test_restart_marks_interrupted_jobs_failed(workspace):
    service = Workbench(workspace)
    try:
        run = service.create("14B")
        state = service.read(run["id"])
        state["jobs"] = [
            {"id": "interrupted", "layer": "TB_L1", "status": "running"},
            {"id": "queued", "layer": "TB_L2", "status": "queued"},
            {"id": "finished", "layer": "TB_L0", "status": "succeeded"},
        ]
        service.save(state)
    finally:
        service.close()
    reopened = Workbench(workspace)
    try:
        jobs = reopened.read(run["id"])["jobs"]
        assert [j["status"] for j in jobs] == ["failed", "failed", "succeeded"]
        assert "重启" in jobs[0]["error"]
        assert (reopened.root(run["id"]) / "jobs/interrupted/job.json").is_file()
    finally:
        reopened.close()


def test_reported_success_without_new_output_is_failed(workbench, monkeypatch):
    run = workbench.create("14B")
    monkeypatch.setattr(workbench.adapter, "execute", lambda *a, **k: {"success": True})
    job = settled(workbench, run["id"], workbench.submit(run["id"], {"layer": "TB_L2"}))
    assert job["status"] == "failed"
    assert "历史文件" in job["error"]
    view = workbench.view(run["id"])
    assert view["stages"]["TB_L2"]["history_count"] == 1
    assert view["stages"]["TB_L2"]["current_count"] == 0
    assert view["tb_version"] == run["tb_version"]


def test_deterministic_refit_rewriting_identical_bytes_is_successful(workbench, monkeypatch):
    run = workbench.create("14B")

    def recompute(payload, layer, output, config, tb):
        destination = output / "14B/tb/L2/json/2601010000_14B_TB.l2.json"
        previous = destination.read_bytes()
        time.sleep(0.01)
        destination.write_bytes(previous)
        return {"success": True}

    monkeypatch.setattr(workbench.adapter, "execute", recompute)
    job = settled(workbench, run["id"], workbench.submit(run["id"], {"layer": "TB_L2"}))
    assert job["status"] == "succeeded", job.get("error")
    updated = workbench.view(run["id"])
    assert updated["tb_version"]["sha256"] == run["tb_version"]["sha256"]
    assert updated["tb_version"]["origin"] == "computed"
    assert updated["stages"]["TB_L2"]["current_count"] == 1


def test_failed_attempt_keeps_outputs_isolated_and_history_intact(workbench, monkeypatch):
    run = workbench.create("14B")
    old_path = workbench.root(run["id"]) / "products/14B/tb/L2/json/2601010000_14B_TB.l2.json"
    before = old_path.read_bytes()

    def fail(payload, layer, output, config, tb):
        put(output / "14B/tb/L2/json/bad_TB.l2.json", [{"G0": 999}])
        return {"success": False, "errors": ["fit did not converge"]}

    monkeypatch.setattr(workbench.adapter, "execute", fail)
    job = settled(workbench, run["id"], workbench.submit(run["id"], {"layer": "TB_L2"}))
    assert job["status"] == "failed" and "fit did not converge" in job["error"]
    assert old_path.read_bytes() == before
    assert not (old_path.parent / "bad_TB.l2.json").exists()
    assert (workbench.root(run["id"]) / "jobs" / job["id"] / "result.json").exists()


def test_ec_l1_can_refresh_an_old_tb_dependency(workbench, monkeypatch):
    run = workbench.create("14B")
    state = workbench.read(run["id"])
    state["ec_dependency"] = {**state["tb_version"], "sha256": "old-version"}
    state["dirty_layers"] = ["EC_L1", "EC_L2", "EC_L3"]
    workbench.save(state)
    with pytest.raises(ValueError, match="TB"):
        workbench.submit(run["id"], {"layer": "EC_L2"})

    def compute(payload, layer, output, config, tb):
        destination = output / "14B/ec/L1/parquet/70keV_ch0_corr.l1.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"new-corrected-data")
        return {"success": True}

    monkeypatch.setattr(workbench.adapter, "execute", compute)
    job = settled(workbench, run["id"], workbench.submit(run["id"], {"layer": "EC_L1"}))
    assert job["status"] == "succeeded", job.get("error")
    updated = workbench.view(run["id"])
    assert updated["ec_dependency"]["sha256"] == updated["tb_version"]["sha256"]
    assert not updated["dependency_stale"]


def test_full_ec_l2_requires_full_ec_l1_coverage(workbench):
    run = workbench.create("14B")
    state = workbench.read(run["id"])
    state["ec_dependency"] = dict(state["tb_version"])
    state["ec_scope"] = "representative"
    workbench.save(state)
    with pytest.raises(ValueError, match="全量 EC L1"):
        workbench.submit(run["id"], {"layer": "EC_L2", "representative": False})
    assert workbench.read(run["id"])["jobs"] == []


@pytest.mark.parametrize("layer,input_layer", [("TB_L1", "TB_L0"), ("EC_L1", "EC_L0")])
def test_jobs_consume_current_run_l0_before_history(workbench, monkeypatch, layer, input_layer):
    run = workbench.create("14B")
    chain, level = input_layer.split("_")
    relative = Path("14B") / chain.lower() / level / "parquet/current.l0.parquet"
    current = workbench.root(run["id"]) / "products" / relative
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_bytes(b"current-computation")

    def compute(payload, requested_layer, output, config, tb):
        assert (output / relative).read_bytes() == b"current-computation"
        pipeline, number = requested_layer.split("_")
        put(output / "14B" / pipeline.lower() / number / "json/new-result.json", {"center": 301})
        return {"success": True}

    monkeypatch.setattr(workbench.adapter, "execute", compute)
    job = settled(workbench, run["id"], workbench.submit(run["id"], {"layer": layer}))
    assert job["status"] == "succeeded", job.get("error")


def test_tb_revision_blocks_downstream_until_refit(workbench):
    run = workbench.create("14B")
    workbench.patch(run["id"], {"excluded": ["14B:20C_285_001:0"]})
    with pytest.raises(ValueError, match="TB"):
        workbench.submit(run["id"], {"layer": "EC_L1"})


def test_single_stem_refit_keeps_other_saved_windows_pending(workbench, workspace, monkeypatch):
    source = workspace / "products/14B/tb/L1/json/20C_285_001_fit_results.l1.json"
    other_stem = "25C_285_002"
    put(source.with_name(f"{other_stem}_fit_results.l1.json"), json.loads(source.read_text()))
    run = workbench.create("14B")
    workbench.patch(
        run["id"],
        {
            "windows": {
                "20C_285_001": {"0": {"lo": 255, "hi": 345}},
                other_stem: {"1": {"lo": 255, "hi": 345}},
            }
        },
    )

    def compute(payload, layer, output, config, tb):
        stem = config["only"][0]
        destination = output / "14B/tb/L1/json" / f"{stem}_fit_results.l1.json"
        records = json.loads(destination.read_text())
        records[0]["center"] = 302
        put(destination, records)
        return {"success": True, "processor": {"outputs": [
            {"stem": stem, "channels_fitted": 4, "channels_updated": [0, 1, 2, 3]}
        ]}}

    monkeypatch.setattr(workbench.adapter, "execute", compute)
    first = settled(
        workbench, run["id"], workbench.submit(run["id"], {"layer": "TB_L1", "stem": "20C_285_001"})
    )
    assert first["status"] == "succeeded", first.get("error")
    state = workbench.read(run["id"])
    assert state["pending_windows"] == [f"14B:{other_stem}:1"]
    assert "TB_L1" in state["dirty_layers"]
    with pytest.raises(ValueError, match="TB L1"):
        workbench.submit(run["id"], {"layer": "TB_L2"})
    second = settled(
        workbench, run["id"], workbench.submit(run["id"], {"layer": "TB_L1", "stem": other_stem})
    )
    assert second["status"] == "succeeded", second.get("error")
    state = workbench.read(run["id"])
    assert state["pending_windows"] == []
    assert "TB_L1" not in state["dirty_layers"]


def test_schema_sanity_failure_is_separate_from_computation(workbench, workspace, monkeypatch):
    checker = workspace / "cali_format/src/check/TB_L2.py"
    checker.parent.mkdir(parents=True)
    checker.write_text('def validate(path):\n    print("CRC sanity failed")\n    return False\n')
    run = workbench.create("14B")

    def compute(payload, layer, output, config, tb):
        records = json.loads(Path(tb).read_text())
        records[0]["k"] = 0.02
        put(output / "14B/tb/L2/json/new_14B_TB.l2.json", records)
        return {"success": True}

    monkeypatch.setattr(workbench.adapter, "execute", compute)
    job = settled(workbench, run["id"], workbench.submit(run["id"], {"layer": "TB_L2"}))
    assert job["status"] == "succeeded", job.get("error")
    assert job["validation"]["status"] == "failed"
    assert job["validation"]["total"] == 1
    view = workbench.view(run["id"])
    assert view["stages"]["TB_L2"]["status"] == "succeeded"
    assert view["stages"]["TB_L2"]["validation"]["status"] == "failed"


@pytest.fixture
def http_server(workbench):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(workbench))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(), dict(response.headers)
    finally:
        connection.close()


def test_http_loopback_host_and_origin_contract(http_server):
    status, body, headers = request(http_server, "GET", "/api/health")
    assert status == 200 and json.loads(body)["status"] == "ok"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert request(http_server, "GET", "/api/health", headers={"Host": "untrusted.example"})[0] == 403
    assert (
        request(http_server, "GET", "/api/health", headers={"Origin": "https://untrusted.example"})[0] == 403
    )
    host = f"127.0.0.1:{http_server.server_port}"
    assert (
        request(http_server, "GET", "/api/health", headers={"Host": host, "Origin": f"http://{host}"})[0]
        == 200
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/history/%2e%2e/secret.txt",
        "/api/history/%2e%2e%5csecret.txt",
        "/static/%2e%2e/server.py",
    ],
)
def test_http_files_cannot_escape_allowed_roots(http_server, workspace, path):
    (workspace / "secret.txt").write_text("private-file")
    status, body, _ = request(http_server, "GET", path)
    assert status == 404
    assert b"private-file" not in body


def test_http_malformed_and_non_object_bodies_are_bad_requests(http_server):
    for body in ("{broken", "[1, 2, 3]"):
        assert (
            request(
                http_server, "POST", "/api/runs", body=body, headers={"Content-Type": "application/json"}
            )[0]
            == 400
        )
    assert request(http_server, "POST", "/api/runs", body='{"payload":"wrong"}')[0] == 400


def test_http_run_create_and_configuration_are_usable(http_server):
    status, body, _ = request(http_server, "POST", "/api/runs", body='{"payload":"14B","name":"http demo"}')
    assert status == 201
    run = json.loads(body)
    status, body, _ = request(
        http_server,
        "PATCH",
        f"/api/runs/{run['id']}/config",
        body='{"excluded":["14B:20C_285_001:0"],"reason":"HTTP review"}',
    )
    assert status == 200 and json.loads(body)["config_revision"] == 1
    assert request(http_server, "GET", f"/api/runs/{run['id']}")[0] == 200
    assert request(http_server, "GET", f"/api/runs/{run['id']}/files/../../secret.txt")[0] == 404
