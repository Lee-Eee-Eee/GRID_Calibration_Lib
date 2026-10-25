"""Run-scoped acquisition versions preserve sources and prevent mixed standards."""

import copy
import hashlib
import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pandas as pd
import pytest
from calibration_lib.common.utils import write_parquet
from calibration_lib.workbench.acquisition_store import presets
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.server import make_handler
from calibration_lib.workbench.service import LAYERS, Workbench


@pytest.fixture
def acquisition_workspace(tmp_path):
    raw = tmp_path / "14、15B标定数据/02/Temp_Vbias"
    raw.mkdir(parents=True)
    (raw.parent / "X_new").mkdir()
    (raw.parent / "source").mkdir()
    for key, counter in (("20C_285", "001"), ("30C_290", "002")):
        (raw / f"{key}_{counter}.event.dat").write_bytes(b"event-packets")
        (raw / f"{key}_ecu_{counter}.hk").write_bytes(b"housekeeping-packets")
    source = Path(presets()[0]["path"])
    (tmp_path / "payload-protocol.xml").write_bytes(source.read_bytes())
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
    product = tmp_path / "products/14B/tb/L2/json/2601010000_14B_TB.l2.json"
    product.parent.mkdir(parents=True)
    product.write_text(json.dumps([params] * 4), encoding="utf-8")
    return tmp_path


@pytest.fixture
def acquisition_service(acquisition_workspace):
    service = Workbench(acquisition_workspace)
    yield service
    service.close()


def draft(service, run_id):
    profile = service.acquisition(run_id)["profile"]
    for role in ("event", "hk"):
        profile["xml"][role] = {"source_path": "payload-protocol.xml", "text": None}
    return profile


def file_hashes(directory):
    return {
        p.relative_to(directory): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file()
    }


def settled(service, run_id, submitted):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = service.job(run_id, submitted["id"])
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    pytest.fail("Acquisition test job did not settle")


def test_default_profile_preview_is_read_only_and_uses_hk(acquisition_service):
    service = acquisition_service
    run = service.create("14B")
    state_path = service.root(run["id"]) / "state.json"
    before = state_path.read_bytes()
    result = service.acquisition(run["id"])
    assert result["revision"] == 0
    assert len(result["sha256"]) == 64
    assert result["profile"]["parser"]["conditions"]["mode"] == "hk"
    assert all(result["profile"]["xml"][role]["text"] for role in ("event", "hk"))
    preview = service.preview_acquisition(run["id"], result["profile"])
    assert preview["errors"] == []
    assert preview["counts"] == {"tb": 2}
    assert {row["stem"] for row in preview["rows"]} == {"20C_285_001", "30C_290_002"}
    assert preview["rows"][0]["temp"] == 20
    assert preview["rows"][0]["bias"] == 28.5
    assert state_path.read_bytes() == before
    assert not (service.root(run["id"]) / "acquisition").exists()


def test_saved_snapshots_and_revisions_restore_without_mutating_inputs(acquisition_workspace):
    workspace = acquisition_workspace
    original = {name: file_hashes(workspace / name) for name in ("products", "14、15B标定数据")}
    xml_before = (workspace / "payload-protocol.xml").read_bytes()
    service = Workbench(workspace)
    try:
        run = service.create("14B")
        saved = service.save_acquisition(run["id"], draft(service, run["id"]), "new payload protocol")
        first = saved["acquisition"]
        assert first["revision"] == 1
        assert set(saved["acquisition_pending_layers"]) == set(LAYERS)
        assert set(saved["dirty_layers"]) == set(LAYERS)
        assert saved["ec_dependency"] is None
        assert saved["config_revision"] == 1
        assert saved["revisions"][-1]["reason"] == "new payload protocol"
        operation = saved["operations"][-1]
        assert operation["label"] == "保存读取配置"
        assert {item["name"] for item in operation["files"]} == {"0001.json", "profile.json", "event.xml", "hk.xml"}
        for item in operation["files"]:
            assert not Path(item["path"]).is_absolute()
            assert (service.root(run["id"]) / item["path"]).is_file()
            preview = service.artifact_preview(run["id"], item["path"])
            assert preview["name"] == item["name"]
            assert preview["kind"] == ("json" if item["name"].endswith(".json") else "text")
        first_bytes = Path(first["path"]).read_bytes()
        for item in first["xml"]:
            assert Path(item["path"]).is_relative_to(service.root(run["id"]))
            assert Path(item["path"]).read_bytes() == (workspace / "payload-protocol.xml").read_text(
                encoding="utf-8-sig"
            ).encode("utf-8")
        changed = service.acquisition(run["id"])["profile"]
        changed["parser"]["conditions"].update(mode="fixed", temp=25.0, bias=28.5)
        second = service.save_acquisition(run["id"], changed, "adopt fixed test condition")
        assert second["acquisition"]["revision"] == 2
        assert second["acquisition"]["sha256"] != first["sha256"]
        assert Path(first["path"]).read_bytes() == first_bytes
        assert (service.root(run["id"]) / "revisions/0002.json").is_file()
        same = service.save_acquisition(run["id"], service.acquisition(run["id"])["profile"])
        assert same["acquisition"] == second["acquisition"]
        assert same["config_revision"] == 2
    finally:
        service.close()
    reopened = Workbench(workspace)
    try:
        restored = reopened.acquisition(run["id"])
        assert restored["revision"] == 2
        assert restored["sha256"] == second["acquisition"]["sha256"]
        assert restored["profile"]["parser"]["conditions"]["mode"] == "fixed"
        assert reopened.view(run["id"])["acquisition_pending_layers"] == second["acquisition_pending_layers"]
    finally:
        reopened.close()
    assert (workspace / "payload-protocol.xml").read_bytes() == xml_before
    assert original == {name: file_hashes(workspace / name) for name in original}


@pytest.mark.parametrize("invalid", ["regex", "xml", "field", "manifest"])
def test_invalid_profile_leaves_durable_state_and_snapshot_tree_unchanged(acquisition_service, invalid):
    service = acquisition_service
    run = service.create("14B")
    service.save_acquisition(run["id"], draft(service, run["id"]))
    profile = service.acquisition(run["id"])["profile"]
    if invalid == "regex":
        profile["discovery"]["rules"][0]["event_regex"] = "["
    elif invalid == "xml":
        profile["xml"]["event"]["text"] = "<broken>"
    elif invalid == "field":
        profile["parser"]["fields"]["channel"] = "unknown_channel_field"
    else:
        profile["discovery"].update(
            mode="manifest",
            manifest=[
                {
                    "kind": "tb",
                    "event": "missing-event.bin",
                    "hk": "missing-hk.bin",
                    "temp": 20,
                    "bias": 28.5,
                }
            ],
        )
    state_path = service.root(run["id"]) / "state.json"
    before = state_path.read_bytes()
    snapshots = file_hashes(service.root(run["id"]) / "acquisition")
    with pytest.raises(ValueError):
        service.save_acquisition(run["id"], profile)
    assert state_path.read_bytes() == before
    assert file_hashes(service.root(run["id"]) / "acquisition") == snapshots


@pytest.mark.parametrize("status", ["queued", "running"])
def test_running_or_queued_calculation_blocks_profile_save(acquisition_service, status):
    service = acquisition_service
    run = service.create("14B")
    profile = draft(service, run["id"])
    state = service.read(run["id"])
    state["jobs"] = [{"id": "active-job", "status": status, "layer": "TB_L0"}]
    service.save(state)
    state_path = service.root(run["id"]) / "state.json"
    before = state_path.read_bytes()
    with pytest.raises(ValueError, match="计算运行中"):
        service.save_acquisition(run["id"], profile)
    assert state_path.read_bytes() == before
    assert not (service.root(run["id"]) / "acquisition").exists()


def test_partial_recomputation_retains_pending_coverage_and_captures_job_version(
    acquisition_service,
    monkeypatch,
):
    service = acquisition_service
    run = service.create("14B")
    saved = service.save_acquisition(run["id"], draft(service, run["id"]))
    expected = saved["inventory"]["raw"]["reading_preview"]["rows"]
    first, second = [row["stem"] for row in expected]

    def execute(adapter, payload, layer, output, config, _tb):
        for stem in config.get("only") or [row["stem"] for row in expected]:
            path = output / payload / "tb/L0/parquet" / (stem + ".l0.parquet")
            write_parquet(
                path,
                pd.DataFrame({"channel": [0], "amp": [100.0]}),
                metadata={"profile_sha256": adapter.acquisition["_sha256"]},
                meta_key="acquisition",
            )
        return {"success": True}

    monkeypatch.setattr(ComputeAdapter, "execute", execute)
    partial = settled(service, run["id"], service.submit(run["id"], {"layer": "TB_L0", "stem": first}))
    assert partial["status"] == "succeeded", partial.get("error")
    assert partial["acquisition"] == saved["acquisition"]
    coverage = service.view(run["id"])["stages"]["TB_L0"]["acquisition_coverage"]
    assert coverage == {
        "completed": 1,
        "required": 2,
        "missing": [second],
        "current": False,
        "sha256": saved["acquisition"]["sha256"],
    }
    assert "TB_L0" in service.view(run["id"])["acquisition_pending_layers"]
    assert "TB_L0" in service.view(run["id"])["dirty_layers"]
    complete = settled(service, run["id"], service.submit(run["id"], {"layer": "TB_L0", "stem": second}))
    assert complete["status"] == "succeeded", complete.get("error")
    view = service.view(run["id"])
    assert view["stages"]["TB_L0"]["acquisition_coverage"]["current"] is True
    assert "TB_L0" not in view["acquisition_pending_layers"]
    assert "TB_L1" in view["acquisition_pending_layers"]


@pytest.mark.parametrize("old_metadata", [{}, {"profile_sha256": "0" * 64}])
def test_partial_current_inputs_cannot_silently_include_old_standard(acquisition_service, old_metadata):
    service = acquisition_service
    run = service.create("14B")
    saved = service.save_acquisition(run["id"], draft(service, run["id"]))
    adapter = service.adapter_for(saved)
    root = service.root(run["id"]) / "products"
    for stem, metadata in (
        ("20C_285_001", {"profile_sha256": saved["acquisition"]["sha256"]}),
        ("30C_290_002", old_metadata),
    ):
        write_parquet(
            root / "14B/tb/L0/parquet" / (stem + ".l0.parquet"),
            pd.DataFrame({"channel": [0], "amp": [100.0]}),
            metadata=metadata,
            meta_key="acquisition",
        )
    adapter._verify_acquisition_inputs("14B", root, "TB_L0", ["20C_285_001"])
    with pytest.raises(ValueError, match="旧读取标准|缺少配置来源"):
        adapter._verify_acquisition_inputs("14B", root, "TB_L0", None)


@pytest.fixture
def acquisition_http(acquisition_service):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(acquisition_service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def http_json(server, method, path, body=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=10)
    try:
        connection.request(
            method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_http_xml_defaults_preview_and_save_contract(acquisition_http):
    status, defaults = http_json(acquisition_http, "GET", "/api/acquisition/defaults?profile=15B")
    assert status == 200
    assert defaults["profile"]["discovery"]["rules"][1]["directory"] == "X"
    status, xml = http_json(acquisition_http, "POST", "/api/xml/read", {"preset": "hnu_packet"})
    assert status == 200
    assert {"grid1x_ft_packet", "hk_packet"} <= set(xml["tags"])
    assert len(xml["sha256"]) == 64
    status, run = http_json(acquisition_http, "POST", "/api/runs", {"payload": "14B"})
    assert status == 201
    base = f"/api/runs/{run['id']}/acquisition"
    status, loaded = http_json(acquisition_http, "GET", base)
    assert status == 200 and loaded["revision"] == 0
    status, preview = http_json(acquisition_http, "POST", base + "/preview", {"profile": loaded["profile"]})
    assert status == 200 and preview["counts"] == {"tb": 2}
    status, saved = http_json(
        acquisition_http, "PATCH", base, {"profile": loaded["profile"], "reason": "HTTP protocol review"}
    )
    assert status == 200 and saved["acquisition"]["revision"] == 1
    assert saved["revisions"][-1]["reason"] == "HTTP protocol review"
    invalid = copy.deepcopy(loaded["profile"])
    invalid["xml"]["hk"]["text"] = "invalid XML"
    status, error = http_json(acquisition_http, "POST", base + "/preview", {"profile": invalid})
    assert status == 400 and "XML" in error["error"]
