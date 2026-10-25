"""Persist source-line edits, fit draft points, and inspect their saved files."""

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.workbench.server import make_handler
from calibration_lib.workbench.service import Workbench


def tb_records():
    return [{"G0": .012, "k": .0184, "V0": 24.11, "b": 54.31, "c": 23459.83,
             "G0_err": .001, "k_err": .001, "V0_err": .01, "b_err": .1,
             "c_err": 1., "chisquare": 1.} for _ in range(4)]


@pytest.fixture
def peak_service(tmp_path):
    products = tmp_path / "products/14B"
    write_json(products / "tb/L2/json/2601010000_14B_TB.l2.json", tb_records())
    parameters = {str(channel): {key: row[key] for key in ("G0", "k", "V0", "b", "c")}
                  for channel, row in enumerate(tb_records())}
    metadata = {"tb_params_used": parameters, "tb_corrfile": "recorded_TB.l2.json",
                "peak_energies_keV": [511., 1274.5], "exposure_minutes": 5.}
    random = np.random.default_rng(851)
    signals, backgrounds = [], []
    for channel in range(4):
        amplitude = np.r_[random.normal(300., 9., 1800), random.normal(610., 12., 2300),
                          random.uniform(50, 750, 350)]
        signals.append(pd.DataFrame({"channel": channel, "amp_corr": amplitude, "crc_check": True,
                                     "utc": np.linspace(0, 120, len(amplitude)), "temp": 25., "bias": 28.5}))
        background = random.uniform(50, 750, 500)
        backgrounds.append(pd.DataFrame({"channel": channel, "amp_corr": background, "crc_check": True,
                                         "utc": np.linspace(0, 120, len(background)), "temp": 25., "bias": 28.5}))
    signal_path = products / "ec/L1/parquet/src_Na22_5m_corr.l1.parquet"
    write_parquet(signal_path, pd.concat(signals), metadata=metadata, meta_key="tb_correction")
    write_parquet(products / "ec/L1/parquet/src_bkg_30m_corr.l1.parquet", pd.concat(backgrounds),
                  metadata={"tb_params_used": parameters, "exposure_minutes": 30.}, meta_key="tb_correction")
    records = [{"channel": channel, "E": energy, "center": center, "center_err": .4,
                "sigma": 10., "sigma_err": .3, "amplitude": 500., "amplitude_err": 5., "fwhm": 23.55,
                "height": 10., "resolution": 8., "resolution_err": .2, "rsquared": .99,
                "fit_mode": "gaus", "source": "src", "source_name": "Na22", "temp": 25., "temp_err": .1,
                "bias": 28.5, "bias_err": .01, "lo": center - 50., "hi": center + 50.,
                "background_channel": channel, "bin_width": 4., "signal_file": signal_path.name,
                "background_file": "src_bkg_30m_corr.l1.parquet", "fit_quality": {"accepted": True},
                "review_note": f"keep {channel}/{energy}"}
               for channel in range(4) for energy, center in ((511., 300.), (1274.5, 610.))]
    historical = products / "ec/L2/json/src_Na22_fit_results.l2.json"
    write_json(historical, records)
    service = Workbench(tmp_path)
    dataset = service.scan_datasets(product_dir=str(products), payload="14B")["datasets"][0]
    run = service.create("14B", dataset_id=dataset["id"])
    try:
        yield service, run["id"], historical
    finally:
        service.close()


def source_edit():
    return {"energy_key": "src_Na22", "source_name": "Na22", "lines": [
        {"energy_keV": 511., "windows": {"1": {"lo": 250., "hi": 350.}}},
        {"energy_keV": 1274.5, "windows": {"1": {"lo": 555., "hi": 665.}}},
        {"energy_keV": 1300., "windows": {"1": {"lo": 550., "hi": 670.}}, "centers": {"1": 610.}},
    ]}


def settle(service, run_id, spec):
    submitted = service.submit(run_id, spec)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = service.job(run_id, submitted["id"])
        if job["status"] not in ("queued", "running"):
            assert job["status"] == "succeeded", job.get("error")
            return job
        time.sleep(.02)
    pytest.fail("Source peak job did not settle")


def test_http_preloads_lines_saves_config_and_reopens_a_real_draft_spectrum(peak_service):
    service, run_id, _historical = peak_service
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, suffix, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=30)
        connection.request(method, f"/api/runs/{run_id}/{suffix}",
                           body=json.dumps(data) if data is not None else None,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        assert response.status == 200, body
        return body

    try:
        preload = request("GET", "ec-peak-config")
        assert [line["energy_keV"] for line in preload["spectra"][0]["lines"]] == [511., 1274.5]
        saved = request("PATCH", "ec-peak-config", source_edit())
        assert saved["saved_files"][0]["name"].endswith("_src_Na22.json")
        preview = service.artifact_preview(run_id, saved["saved_files"][0]["path"])
        assert preview["kind"] == "json" and preview["json"]["energy_key"] == "src_Na22"
        assert len(preview["json"]["lines"]) == 3
        assert preview["json"]["lines"][0]["windows"]["1"]["lo"] == 250.
        assert service.view(run_id, compact=True)["operations"][-1]["files"] == saved["saved_files"]
        draft_id = "14B:ec:src_Na22:1:1300"
        point = next(row for row in service.ec_overview(run_id, 1)["points"] if row["id"] == draft_id)
        assert point["fit_status"] == "not_fitted" and point["center"] is None
        spectrum = service.ec_spectrum(run_id, "EC_L2", 1, point_id=draft_id)
        assert spectrum["fit"] is None and spectrum["curve"] is None
        assert len(spectrum["x"]) == len(spectrum["y"]) > 20
        assert spectrum["n_events"] > 4000
        assert spectrum["draft_window"] == {"lo": 550., "hi": 670.}
        # Exercise a new reader, not a cached reference to this response.
        service.peak_catalog_cache.clear()
        reopened = request("GET", "ec-peak-config")
        assert reopened["spectra"][0]["lines"][-1]["energy_keV"] == 1300.
        assert service.read(run_id)["config"]["ec_peak_sets"]["src_Na22"]["lines"][-1]["centers"] == {"1": 610.}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_new_point_and_current_channel_source_fit_preserve_peers_and_frozen_job_preview(peak_service):
    service, run_id, historical = peak_service
    historical_bytes = historical.read_bytes()
    service.save_ec_peak_config(run_id, source_edit())
    output = service.root(run_id) / "products/14B/ec/L2/json/src_Na22_fit_results.l2.json"
    before = read_json(output)
    first = settle(service, run_id, {"layer": "EC_L2", "point_id": "14B:ec:src_Na22:1:1300", "channel": 1})
    assert first["result"]["processor"]["outputs"][0]["points_updated"] == ["14B:ec:src_Na22:1:1300"]
    after = read_json(output)
    assert after[:len(before)] == before
    added = next(row for row in after if row["E"] == 1300.)
    assert added["center"] == pytest.approx(610., abs=2.)
    assert (added["lo"], added["hi"]) == (550., 670.)
    second = settle(service, run_id, {"layer": "EC_L2", "energy_key": "src_Na22", "fit_channels": [1]})
    updated = second["result"]["processor"]["outputs"][0]["points_updated"]
    assert updated == [f"14B:ec:src_Na22:1:{energy:g}" for energy in (511., 1274.5, 1300.)]
    state = service.read(run_id)
    assert not any(point.split(":")[3] == "1" for point in state["pending_ec_windows"])
    assert {f"14B:ec:src_Na22:{channel}:1300" for channel in (0, 2, 3)} <= set(state["pending_ec_windows"])
    assert "EC_L2" in state["dirty_layers"]
    with pytest.raises(ValueError, match="EC L2"):
        service.submit(run_id, {"layer": "EC_L3"})
    records = read_json(output)
    assert all(record in records for record in before if record["channel"] != 1)
    compact = service.job(run_id, second["id"], compact=True)
    assert compact["outputs"] and compact["result"]["counts"]["processed"] == 1
    assert "outputs" not in compact["result"]["processor"]
    assert service.view(run_id, compact=True)["jobs"][-1]["outputs"] == compact["outputs"]
    relative = "14B/ec/L2/json/src_Na22_fit_results.l2.json"
    frozen = service.artifact_preview(run_id, relative, job_id=first["id"])
    assert frozen["kind"] == "json" and frozen["name"] == output.name
    records[0]["center"] = 99999.
    write_json(output, records)
    current = service.artifact_preview(run_id, relative)
    assert current["json"][0]["center"] == 99999.
    assert service.artifact_preview(run_id, relative, job_id=first["id"])["json"] == frozen["json"]
    assert historical.read_bytes() == historical_bytes


def test_image_preview_does_not_read_its_binary_and_paths_stay_inside_run(peak_service, monkeypatch):
    service, run_id, _historical = peak_service
    target = service.root(run_id) / "products/14B/ec/L2/figures/large.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 1024)
    original = Path.open

    def guard(self, *args, **kwargs):
        if self.resolve() == target.resolve():
            pytest.fail("Image preview metadata must not read the binary body")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guard)
    preview = service.artifact_preview(run_id, "14B/ec/L2/figures/large.png")
    assert preview["kind"] == "image" and preview["image_url"].endswith("large.png")
    assert preview["size"] == 1032
    for path in ("../../state.json", str(target.resolve())):
        with pytest.raises((ValueError, FileNotFoundError)):
            service.artifact_preview(run_id, path)


@pytest.mark.parametrize("pending", ["src_Na22_5m", "src_bkg_30m"])
def test_current_source_fit_waits_for_updated_signal_or_background_correction(peak_service, pending):
    service, run_id, _historical = peak_service
    state = service.read(run_id)
    state["pending_ec_l0"] = [pending]
    service.save(state)
    with pytest.raises(ValueError, match="EC L1"):
        service.submit(run_id, {"layer": "EC_L2", "energy_key": "src_Na22", "fit_channels": [1]})
    assert service.read(run_id)["jobs"] == []
