"""L0 spectra remain operable before the first successful TB L1 fit."""

import time

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.workbench.acquisition import default_profile
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.service import Workbench

HISTOGRAM = {"min": 0.0, "max": 16384.0, "bin_width": 6.0}


def l0(products, stem="case", profile_sha=None):
    rng = np.random.default_rng(765)
    amp = np.r_[rng.normal(2200, 85, 6000), rng.uniform(1300, 3000, 800)]
    frame = pd.DataFrame(
        {
            "channel": np.repeat(np.arange(4), len(amp)),
            "amp": np.tile(amp, 4),
            "crc_check": True,
            "utc": np.tile(np.linspace(0, 60, len(amp)), 4),
        }
    )
    path = products / "10B/tb/L0/parquet" / f"{stem}.l0.parquet"
    write_parquet(
        path,
        frame,
        metadata={
            "temp": [20, 21, 22, 23],
            "temp_err": [0.1] * 4,
            "bias": [27, 27.5, 28, 28.5],
            "bias_err": [0.01] * 4,
        },
        meta_key="hk_data",
        schema_metadata={
            "acquisition": {
                "profile_sha256": profile_sha,
                "fitting": {"tb_histogram": HISTOGRAM},
            }
        },
    )
    return path, len(amp)


def settled(service, run_id, job):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        current = service.job(run_id, job["id"])
        if current["status"] not in ("running", "queued"):
            return current
        time.sleep(0.02)
    pytest.fail("First-fit integration job did not finish")


def test_l0_only_points_supply_real_hk_and_selectable_unfitted_spectra(tmp_path):
    products = tmp_path / "runs/initial/products"
    path, count = l0(products)
    adapter = ComputeAdapter(tmp_path)
    points = adapter.points("10B", products)
    assert len(points) == 4
    assert {p["channel"] for p in points} == {0, 1, 2, 3}
    for point in points:
        assert point["fit_status"] == ("model_pending" if point["channel"] == 3 else "not_fitted")
        assert point["used"] is False
        assert point["center"] is point["gain"] is point["l1_path"] is None
        assert point["l0_path"] == str(path)
        assert point["temp"] == 20 + point["channel"]
        assert point["bias"] == 27 + point["channel"] * 0.5
    assert points[3]["unsupported_spectral_model"] == "塑料闪烁体 · 康普顿响应，拟合方案待定"
    spectrum = adapter.spectrum("10B", products, "10B:case:2")
    assert spectrum["fit"] is None and spectrum["curve"] is None
    assert spectrum["n_events"] == sum(spectrum["y"]) == count
    assert spectrum["histogram_config"] == HISTOGRAM
    assert spectrum["default_window"] == {"lo": 0, "hi": 16384}
    assert spectrum["window_bounds"] == {"min": 0, "max": 16384, "minimum_width": 48}
    # Unfitted display bounds must not become a manual full-range fit.
    window_file = adapter._windows("10B", {"windows": {}}, products)
    assert "case" not in read_json(window_file)


def test_l0_union_preserves_fitted_rows_and_never_counts_unfitted_points_in_surface(tmp_path):
    products = tmp_path / "runs/initial/products"
    l0(products)
    l0(products, "unfitted")
    record = {
        "channel": 0,
        "center": 2188.0,
        "center_err": 1.0,
        "temp": 25.0,
        "bias": 28.5,
        "rsquared": 0.99,
        "window_lo": 1900.0,
        "window_hi": 2500.0,
        "fit_quality": {"accepted": True},
    }
    write_json(products / "10B/tb/L1/json/case_fit_results.l1.json", [record])
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
    write_json(products / "10B/tb/L2/json/2610201200_10B_TB.l2.json", [params] * 4)
    adapter = ComputeAdapter(tmp_path)
    points = adapter.points("10B", products)
    assert len(points) == 8
    assert all(points[0][key] == value for key, value in record.items())
    assert points[0]["id"] == "10B:case:0"
    assert sum(p["fit_status"] == "not_fitted" for p in points) == 5
    assert sum(p["fit_status"] == "model_pending" for p in points) == 2
    surface = adapter.surface("10B", products, 0)
    assert surface["diagnostics"]["n_points"] == 1
    assert len(surface["points"]) == 2
    assert next(p for p in surface["points"] if p["stem"] == "unfitted")["used"] is False
    assert next(p for p in surface["points"] if p["stem"] == "case")["center"] == 2188.0


def test_failed_first_fit_can_be_inspected_revised_and_refit_with_actual_processor(tmp_path):
    history = tmp_path / "products"
    l0(history)
    service = Workbench(tmp_path)
    try:
        dataset = service.scan_datasets(product_dir=str(history / "10B"))["datasets"][0]
        run = service.create(dataset_id=dataset["id"])
        service.save_acquisition(run["id"], default_profile("10B"))
        state = service.read(run["id"])
        l0(history, profile_sha=state["acquisition"]["sha256"])
        assert len(service.surface(run["id"], 0)["points"]) == 1
        before = service.spectrum(run["id"], "case", 0)
        assert before["fit"] is None and before["fit_source"] == "not_fitted"
        assert before["last_fit_attempt"] is None
        # A bad high-ADC window fails in the actual library peak detector.
        bad_window = {"lo": 12000, "hi": 13000}
        service.patch(run["id"], {"windows": {"case": {"0": bad_window}}})
        failed = settled(
            service,
            run["id"],
            service.submit(run["id"], {"layer": "TB_L1", "stem": "case", "representative": False,
                                      "fit_channels": [0]}),
        )
        assert failed["status"] == "failed" and failed["error"]
        assert service.view(run["id"])["stages"]["TB_L1"]["status"] == "failed"
        after_failure = service.spectrum(run["id"], "case", 0)
        assert after_failure["fit"] is None and after_failure["curve"] is None
        assert after_failure["last_fit_attempt"]["status"] == "failed"
        assert all(p["fit_status"] == "not_fitted" for p in service.surface(run["id"], 0)["points"])
        # The same L0 point accepts a revised window without any dummy L1 JSON.
        bounds = {"lo": 1900.125, "hi": 2510.875}
        updated = service.patch(run["id"], {"windows": {"case": {"0": bounds}}})
        assert updated["config"]["windows"]["case"]["0"] == bounds
        succeeded = settled(
            service,
            run["id"],
            service.submit(run["id"], {"layer": "TB_L1", "stem": "case", "representative": False,
                                      "fit_channels": [0]}),
        )
        assert succeeded["status"] == "succeeded", succeeded.get("error")
        assert succeeded["result"]["processor_class"] == "TBL1Processor"
        fitted = service.spectrum(run["id"], "case", 0)
        assert fitted["fit_source"] == "computed"
        assert fitted["last_fit_attempt"]["status"] == "succeeded"
        assert abs(fitted["fit"]["center"] - 2200) < 20
        assert fitted["fit"]["window_lo"] == bounds["lo"]
        assert fitted["fit"]["window_hi"] == bounds["hi"]
        assert fitted["curve"]["x"] and fitted["histogram_config"] == HISTOGRAM
        assert service.surface(run["id"], 0)["points"][0]["fit_status"] == "fitted"
        # A later failure keeps the saved result as a distinct prior artifact.
        old_center = fitted["fit"]["center"]
        service.patch(run["id"], {"windows": {"case": {"0": bad_window}}})
        failed_again = settled(
            service,
            run["id"],
            service.submit(run["id"], {"layer": "TB_L1", "stem": "case", "representative": False,
                                      "fit_channels": [0]}),
        )
        assert failed_again["status"] == "failed"
        current = service.spectrum(run["id"], "case", 0)
        assert current["fit"]["center"] == old_center
        assert current["last_fit_attempt"]["status"] == "failed"
        assert current["last_fit_attempt"]["id"] == failed_again["id"]
        assert service.view(run["id"])["stages"]["TB_L1"]["status"] == "failed"
    finally:
        service.close()
