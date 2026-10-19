"""Directory selection and independently pinned TB input regression tests."""

import hashlib
import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.server import make_handler
from calibration_lib.workbench.service import Workbench


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def tb_rows(k=0.0184):
    return [
        {
            "G0": 0.012,
            "k": k,
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
        for _ in range(4)
    ]


@pytest.fixture
def dependency_workspace(tmp_path):
    (tmp_path / "14、15B标定数据/02/Temp_Vbias").mkdir(parents=True)
    (tmp_path / "14、15B标定数据/03/X").mkdir(parents=True)
    put(tmp_path / "products/14B/tb/L2/json/2601010000_14B_TB.l2.json", tb_rows())
    (tmp_path / "products/15B/ec/L2/json").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def dependency_service(dependency_workspace):
    service = Workbench(dependency_workspace)
    yield service
    service.close()


def default_run(service):
    dataset = next(item for item in service.scan_datasets()["datasets"] if item["payload"] == "14B")
    return service.create("14B", dataset_id=dataset["id"])


def settled(service, run_id, submitted):
    # Real four-channel fits and plots can take >20 s on a busy Windows host.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = service.job(run_id, submitted["id"])
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    pytest.fail("Background job did not settle")


def test_default_catalog_discovers_both_payloads_and_remembers_explicit_directories(
    dependency_service, dependency_workspace
):
    catalog = dependency_service.scan_datasets()
    assert {item["payload"] for item in catalog["datasets"]} == {"14B", "15B"}
    raw = dependency_workspace / "new_measurement/Temp_Vbias"
    raw.mkdir(parents=True)
    custom = dependency_service.scan_datasets(raw_dir=str(raw.parent), payload="GRID-77")["datasets"][0]
    assert custom["payload"] == "GRID-77"
    assert custom["raw_dir"] == str(raw.parent.resolve())
    assert custom["product_dir"] is None
    assert custom["id"] in {item["id"] for item in dependency_service.bootstrap()["datasets"]}


def test_dataset_payload_label_can_change_without_renaming_source_products(
    dependency_service, dependency_workspace
):
    selected = next(
        item for item in dependency_service.scan_datasets()["datasets"] if item["payload"] == "14B"
    )
    source = dependency_workspace / "products/14B/tb/L2/json/2601010000_14B_TB.l2.json"
    before = source.read_bytes()
    run = dependency_service.create("GRID-20", dataset_id=selected["id"])
    assert run["payload"] == "GRID-20"
    assert run["dataset"]["source_payload"] == "14B"
    assert run["dataset"]["profile"] == "14B"
    assert run["stages"]["TB_L2"]["history_count"] == 1
    assert (dependency_service.root(run["id"]) / "products/GRID-20/tb/L2/json" / source.name).is_file()
    assert source.read_bytes() == before
    assert dependency_service.adapter_for(run).historical_layer("GRID-20", "TB_L2") == source.parent.parent


@pytest.mark.parametrize("mode", ["raw", "product"])
def test_raw_only_or_product_only_dataset_opens_without_tb_package(
    dependency_service, dependency_workspace, mode
):
    if mode == "raw":
        path = dependency_workspace / "raw_only/Temp_Vbias"
        path.mkdir(parents=True)
        options = {"raw_dir": str(path.parent), "payload": "RAW-01"}
    else:
        path = dependency_workspace / "product_only/ec/L2/json"
        path.mkdir(parents=True)
        put(path / "40keV_fit_results.l2.json", [])
        options = {"product_dir": str(path.parents[2]), "payload": "PRODUCT-01"}
    dataset = dependency_service.scan_datasets(**options)["datasets"][0]
    run = dependency_service.create(dataset_id=dataset["id"])
    assert run["selected_tb"] is None
    assert run["tb_version"] is None
    assert run["tb_binding"] == "pinned"
    assert run["jobs"] == []
    with pytest.raises(ValueError, match="TB L2 JSON"):
        dependency_service.submit(run["id"], {"layer": "EC_L1"})
    assert dependency_service.read(run["id"])["jobs"] == []


def test_pinned_tb_allows_ec1_even_when_independent_tb_fit_is_dirty(
    dependency_service, dependency_workspace, monkeypatch
):
    run = default_run(dependency_service)
    selected = run["selected_tb"]
    state = dependency_service.read(run["id"])
    state["dirty_layers"] = ["TB_L1", "TB_L2"]
    dependency_service.save(state)
    calls = []

    def execute(adapter, payload, layer, output, config, tb):
        calls.append((adapter.dataset["id"], payload, layer, str(tb)))
        put(output / payload / "ec/L1/json/correction_attempt.json", {"tb": str(tb)})
        return {"success": True}

    # Explicit dataset jobs instantiate a fresh adapter; patch the class.
    monkeypatch.setattr(ComputeAdapter, "execute", execute)
    result = settled(dependency_service, run["id"], dependency_service.submit(run["id"], {"layer": "EC_L1"}))
    assert result["status"] == "succeeded", result.get("error")
    assert calls == [(run["dataset"]["id"], "14B", "EC_L1", selected["path"])]
    restored = dependency_service.view(run["id"])
    assert restored["ec_dependency"]["sha256"] == selected["sha256"]
    assert not restored["dependency_stale"]
    assert "TB_L2" in restored["dirty_layers"]


def test_external_package_selection_is_frozen_persistent_and_does_not_write_source(
    dependency_service, dependency_workspace
):
    run = default_run(dependency_service)
    external = put(dependency_workspace / "external_versions/custom_acquisition.json", tb_rows(0.019))
    original = external.read_bytes()
    updated = dependency_service.select_tb(run["id"], path=str(external))
    selected = updated["selected_tb"]
    assert selected["origin"] == "external"
    assert selected["source_path"] == str(external.resolve())
    assert selected["sha256"] == hashlib.sha256(original).hexdigest()
    assert Path(selected["path"]).is_relative_to(dependency_service.root(run["id"]) / "dependencies")
    assert Path(selected["path"]).read_bytes() == original
    assert external.read_bytes() == original
    # A later external edit must not silently alter the selected correction.
    put(external, tb_rows(0.02))
    assert Path(selected["path"]).read_bytes() == original
    reopened = Workbench(dependency_workspace)
    try:
        restored = reopened.view(run["id"])
        assert restored["selected_tb"] == selected
        assert any(
            version["selected"] and version["sha256"] == selected["sha256"]
            for version in reopened.tb_versions(run["id"])["versions"]
        )
    finally:
        reopened.close()


def test_switching_tb_version_marks_existing_ec_stale_and_can_select_saved_version(
    dependency_service, dependency_workspace
):
    run = default_run(dependency_service)
    state = dependency_service.read(run["id"])
    state["ec_dependency"] = dict(run["selected_tb"])
    dependency_service.save(state)
    assert not dependency_service.view(run["id"])["dependency_stale"]
    old_id = run["selected_tb"]["id"]
    external = put(dependency_workspace / "external/second.json", tb_rows(0.02))
    changed = dependency_service.select_tb(run["id"], path=str(external))
    assert changed["dependency_stale"]
    assert {"EC_L1", "EC_L2", "EC_L3"} <= set(changed["dirty_layers"])
    with pytest.raises(ValueError, match="过期"):
        dependency_service.submit(run["id"], {"layer": "EC_L2"})
    restored = dependency_service.select_tb(run["id"], version_id=old_id)
    assert restored["selected_tb"]["sha256"] == run["selected_tb"]["sha256"]
    assert not restored["dependency_stale"]


@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        "[]",
        '[{"G0":1}]',
        json.dumps([{**row, "k": None} for row in tb_rows()]),
        json.dumps([{**row, "G0": -1} for row in tb_rows()]),
    ],
)
def test_invalid_external_package_cannot_mutate_state_or_revisions(
    dependency_service, dependency_workspace, content
):
    run = default_run(dependency_service)
    source = dependency_workspace / "invalid.json"
    source.write_text(content, encoding="utf-8")
    state_path = dependency_service.root(run["id"]) / "state.json"
    before = state_path.read_bytes()
    revisions = list((state_path.parent / "revisions").glob("*.json"))
    with pytest.raises((ValueError, TypeError)):
        dependency_service.select_tb(run["id"], path=str(source))
    assert state_path.read_bytes() == before
    assert list((state_path.parent / "revisions").glob("*.json")) == revisions
    assert source.read_text(encoding="utf-8") == content


def test_http_directory_catalog_create_dependency_and_external_history_routes(
    dependency_service, dependency_workspace
):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dependency_service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        encoded = json.dumps(body) if body is not None else None
        connection.request(method, path, body=encoded, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        status, data = response.status, json.loads(response.read().decode("utf-8"))
        connection.close()
        return status, data

    try:
        status, directories = request("GET", "/api/directories?path=" + quote(str(dependency_workspace)))
        assert status == 200 and directories["path"] == str(dependency_workspace)
        status, catalog = request("POST", "/api/datasets/scan", {})
        assert status == 200
        dataset = next(item for item in catalog["datasets"] if item["payload"] == "14B")
        status, run = request("POST", "/api/runs", {"payload": "HTTP-GRID", "dataset_id": dataset["id"]})
        assert status == 201 and run["payload"] == "HTTP-GRID"
        status, versions = request("GET", f"/api/runs/{run['id']}/tb-versions")
        assert status == 200 and versions["versions"]
        external = put(dependency_workspace / "http_external/named.json", tb_rows(0.02))
        status, changed = request("PATCH", f"/api/runs/{run['id']}/dependency", {"path": str(external)})
        assert status == 200 and changed["selected_tb"]["source_path"] == str(external.resolve())
        artifact = next(item for item in run["artifacts"] if item["layer"] == "TB_L2")
        status, history = request("GET", artifact["url"])
        assert status == 200 and history == tb_rows()
        invalid = put(dependency_workspace / "http_external/invalid.json", [])
        status, error = request("PATCH", f"/api/runs/{run['id']}/dependency", {"path": str(invalid)})
        assert status == 400 and "error" in error
        assert dependency_service.view(run["id"])["selected_tb"] == changed["selected_tb"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def mixed_ec_history(workspace, *, xray_k=0.0184, background_k=0.02, include_source=False):
    """Real corrected spectra with one independently stale source background."""
    rng = np.random.default_rng(7321)
    parquet = workspace / "products/14B/ec/L1/parquet"

    def params(k):
        return {
            str(ch): {key: row[key] for key in ("G0", "k", "V0", "b", "c")}
            for ch, row in enumerate(tb_rows(k))
        }

    def metadata(k):
        return {"tb_params_used": params(k), "tb_corrfile": "recorded_TB.l2.json"}

    windows = {}
    for ch in range(4):
        center = 300 + ch * 150
        amp = np.concatenate([rng.normal(center, 10, 2200), rng.uniform(0, 950, 400)])
        frame = pd.DataFrame(
            {"amp_corr": amp, "utc": np.linspace(0, 60, len(amp)), "channel": ch, "temp": 25.0, "bias": 28.5}
        )
        write_parquet(
            parquet / f"40keV_ch{ch}_corr.l1.parquet",
            frame,
            metadata=metadata(xray_k),
            meta_key="tb_correction",
        )
        windows[f"14B:ec:40keV:{ch}:40"] = {"lo": center - 45, "hi": center + 45}
    background = pd.concat(
        [
            pd.DataFrame(
                {
                    "amp_corr": rng.uniform(0, 950, 600),
                    "utc": np.linspace(0, 90, 600),
                    "channel": ch,
                    "temp": 25.0,
                    "bias": 28.5,
                }
            )
            for ch in range(4)
        ]
    )
    write_parquet(
        parquet / "src_bkg_30m_corr.l1.parquet",
        background,
        metadata=metadata(background_k),
        meta_key="tb_correction",
    )
    for energy in (40.0, 45.0, 60.0, 80.0):
        records = [
            {
                "channel": ch,
                "E": energy,
                "center": 6 * energy + ch,
                "center_err": 1.0,
                "sigma": 5.0,
                "sigma_err": 0.5,
                "amplitude": 20.0,
                "amplitude_err": 0.2,
                "fwhm": 11.77,
                "resolution": 10.0,
                "resolution_err": 0.5,
                "fit_mode": "lin + gaus",
                "source": "x",
                "source_name": None,
                "temp": 25.0,
                "temp_err": 0.1,
                "bias": 28.5,
                "bias_err": 0.01,
                "lo": 100.0,
                "hi": 900.0,
                "rsquared": 0.99,
                "background_channel": (ch + 1) % 4,
            }
            for ch in range(4)
        ]
        put(workspace / f"products/14B/ec/L2/json/{energy:g}keV_fit_results.l2.json", records)
    if include_source:
        signal = background.copy()
        signal["amp_corr"] = rng.normal(500, 14, len(signal))
        write_parquet(
            parquet / "src_Na22_5m_corr.l1.parquet",
            signal,
            metadata={**metadata(0.0184), "peak_energies_keV": [511.0]},
            meta_key="tb_correction",
        )
        records = [
            {
                "channel": ch,
                "E": 511.0,
                "center": 500.0,
                "center_err": 1.0,
                "sigma": 14.0,
                "sigma_err": 1.0,
                "amplitude": 20.0,
                "amplitude_err": 0.2,
                "fwhm": 33.0,
                "resolution": 6.6,
                "resolution_err": 0.5,
                "fit_mode": "lin + gaus",
                "source": "src",
                "source_name": "Na22",
                "temp": 25.0,
                "temp_err": 0.1,
                "bias": 28.5,
                "bias_err": 0.01,
                "lo": 440.0,
                "hi": 575.0,
                "rsquared": 0.99,
                "background_channel": ch,
            }
            for ch in range(4)
        ]
        put(workspace / "products/14B/ec/L2/json/src_Na22_fit_results.l2.json", records)
    return windows


def test_partial_import_allows_matching_xray_local_refit_despite_unrelated_stale_background(
    dependency_service, dependency_workspace, monkeypatch
):
    windows = mixed_ec_history(dependency_workspace)
    run = default_run(dependency_service)
    assert run["ec_dependency"] is None  # The global import cannot claim all files match.
    dependency_service.patch(run["id"], {"ec_windows": windows})
    result = settled(
        dependency_service,
        run["id"],
        dependency_service.submit(
            run["id"],
            {
                "layer": "EC_L2",
                "point_id": "14B:ec:40keV:0:40",
                "channel": 0,
            },
        ),
    )
    assert result["status"] == "succeeded", result.get("error")
    state = dependency_service.view(run["id"])
    assert state["ec_dependency"] is None  # Local success does not upgrade the global claim.
    assert state["ec_coverage"]["computed_files"] == 1
    assert state["ec_coverage"]["total"] == 4 and state["ec_coverage"]["mixed"]
    assert state["pending_ec_windows"] == []
    assert "EC_L2" not in state["dirty_layers"]
    assert "EC_L3" in state["dirty_layers"]


@pytest.mark.parametrize("kind", ["xray", "source_background"])
def test_local_ec_refit_rejects_mismatched_actual_input_without_overwriting_imported_peaks(
    dependency_service, dependency_workspace, monkeypatch, kind
):
    mixed_ec_history(
        dependency_workspace,
        xray_k=0.02 if kind == "xray" else 0.0184,
        include_source=kind == "source_background",
    )
    run = default_run(dependency_service)
    stem = "40keV" if kind == "xray" else "src_Na22"
    energy = "40" if kind == "xray" else "511"
    root = dependency_service.root(run["id"])
    original = root / f"products/14B/ec/L2/json/{stem}_fit_results.l2.json"
    before = original.read_bytes()
    monkeypatch.setattr(
        ECL2Processor,
        "_fit_energy_spectrum",
        lambda *args, **kwargs: pytest.fail("TB metadata must be checked before fitting"),
    )
    result = settled(
        dependency_service,
        run["id"],
        dependency_service.submit(
            run["id"],
            {
                "layer": "EC_L2",
                "point_id": f"14B:ec:{stem}:0:{energy}",
                "channel": 0,
            },
        ),
    )
    assert result["status"] == "failed"
    assert "Stale TB dependency" in result["error"]
    assert ("40keV_ch" if kind == "xray" else "src_bkg_30m") in result["error"]
    assert original.read_bytes() == before
    assert not dependency_service.read(run["id"])["ec_l2_provenance"]


def test_historical_l3_recalculation_preserves_unknown_tb_provenance_and_warns_about_mixed_inputs(
    dependency_service, dependency_workspace, monkeypatch
):
    mixed_ec_history(dependency_workspace)
    run = default_run(dependency_service)
    assert run["ec_dependency"] is None
    monkeypatch.setattr(ECL3Processor, "_plot_ec_fit", lambda *args: None)
    monkeypatch.setattr(ECL3Processor, "_plot_resolution_fit", lambda *args: None)
    result = settled(dependency_service, run["id"], dependency_service.submit(run["id"], {"layer": "EC_L3"}))
    assert result["status"] == "succeeded", result.get("error")
    coverage = result["ec_coverage"]
    assert coverage["mixed"] and coverage["computed_files"] == 0 and coverage["total"] == 4
    assert all(
        not source["current_tb"] and source["origin"] == "historical" for source in coverage["sources"]
    )
    assert coverage["description"] in result["result"]["warnings"]
    assert result["result"]["ec_coverage"] == coverage
    assert dependency_service.view(run["id"])["ec_dependency"] is None


def test_l3_can_fit_available_l2_without_any_tb_package(
    dependency_service, dependency_workspace, monkeypatch
):
    mixed_ec_history(dependency_workspace)
    (dependency_workspace / "products/14B/tb/L2/json/2601010000_14B_TB.l2.json").unlink()
    run = default_run(dependency_service)
    assert run["selected_tb"] is None
    monkeypatch.setattr(ECL3Processor, "_plot_ec_fit", lambda *args: None)
    monkeypatch.setattr(ECL3Processor, "_plot_resolution_fit", lambda *args: None)
    result = settled(dependency_service, run["id"], dependency_service.submit(run["id"], {"layer": "EC_L3"}))
    assert result["status"] == "succeeded", result.get("error")
    assert result["tb_version"] is None
    assert result["ec_coverage"]["mixed"]


@pytest.mark.parametrize("valid_older", [True, False])
def test_invalid_history_tb_does_not_prevent_loading_other_products(
    dependency_service, dependency_workspace, valid_older
):
    directory = dependency_workspace / "products/14B/tb/L2/json"
    older = directory / "2601010000_14B_TB.l2.json"
    if not valid_older:
        put(older, [{}, {}, {}, {}])
    invalid = put(directory / "9912310000_14B_TB.l2.json", [{}, {}, {}, {}])
    put(dependency_workspace / "products/14B/ec/L2/json/40keV_fit_results.l2.json", [])
    original = invalid.read_bytes()
    run = default_run(dependency_service)
    assert run["import_warnings"] and any(invalid.name in warning for warning in run["import_warnings"])
    assert run["stages"]["EC_L2"]["history_count"] == 1
    assert dependency_service.root(run["id"]).joinpath("state.json").is_file()
    if valid_older:
        assert run["selected_tb"]["filename"] == older.name
    else:
        assert run["selected_tb"] is None and run["tb_version"] is None
    assert invalid.read_bytes() == original


@pytest.mark.parametrize("layer", ["EC_L1", "EC_L2"])
def test_corrupted_selected_tb_snapshot_is_rejected_before_queuing(
    dependency_service, dependency_workspace, layer
):
    run = default_run(dependency_service)
    state_path = dependency_service.root(run["id"]) / "state.json"
    before = state_path.read_bytes()
    put(Path(run["selected_tb"]["path"]), tb_rows(0.02))
    spec = {"layer": layer}
    if layer == "EC_L2":
        spec.update(point_id="14B:ec:40keV:0:40", channel=0)
    with pytest.raises(ValueError, match="SHA-256"):
        dependency_service.submit(run["id"], spec)
    assert state_path.read_bytes() == before
    assert dependency_service.read(run["id"])["jobs"] == []
    assert not list((state_path.parent / "jobs").glob("*/job.json"))


def test_ec_l0_changes_require_matching_ec_l1_without_blocking_unrelated_energies(
    dependency_service, dependency_workspace, monkeypatch
):
    mixed_ec_history(dependency_workspace)
    run = default_run(dependency_service)
    frame = pd.DataFrame({"channel": [0], "amp": [300.0], "crc_check": [True]})

    def execute(adapter, payload, layer, output, config, tb):
        if layer == "EC_L0":
            for stem in ("40keV_ch0", "45keV_ch0"):
                write_parquet(output / payload / "ec/L0/parquet" / f"{stem}.l0.parquet", frame)
        elif layer == "EC_L1":
            for stem in config["only"]:
                write_parquet(output / payload / "ec/L1/parquet" / f"{stem}_corr.l1.parquet", frame)
        else:
            pytest.fail("The final EC L2 eligibility check must not run a calculation")
        return {"success": True}

    monkeypatch.setattr(ComputeAdapter, "execute", execute)
    parsed = settled(dependency_service, run["id"], dependency_service.submit(run["id"], {"layer": "EC_L0"}))
    assert parsed["status"] == "succeeded"
    assert set(dependency_service.read(run["id"])["pending_ec_l0"]) == {"40keV_ch0", "45keV_ch0"}
    spec = {"layer": "EC_L2", "point_id": "14B:ec:40keV:0:40", "channel": 0}
    with pytest.raises(ValueError, match="EC L0 文件已更新"):
        dependency_service.submit(run["id"], spec)
    corrected = settled(
        dependency_service,
        run["id"],
        dependency_service.submit(run["id"], {"layer": "EC_L1", "stem": "40keV_ch0"}),
    )
    assert corrected["status"] == "succeeded"
    assert dependency_service.read(run["id"])["pending_ec_l0"] == ["45keV_ch0"]
    with pytest.raises(ValueError, match="45keV_ch0"):
        dependency_service.submit(run["id"], {**spec, "point_id": "14B:ec:45keV:0:45"})
    monkeypatch.setattr(dependency_service.executor, "submit", lambda *args, **kwargs: None)
    submitted = dependency_service.submit(run["id"], spec)
    assert submitted["status"] == "queued"
