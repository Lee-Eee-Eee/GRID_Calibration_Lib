"""Suspect TB fits keep their parameters and continue through aggregation."""

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.fit_policy import quality_warnings, usable_peak
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.tb.processors.L2_processor import TBL2Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.service import Workbench

PARAMS = {"G0": .012, "k": .0184, "V0": 24.11, "b": 54.31, "c": 23459.83,
          "G0_err": .001, "k_err": .001, "V0_err": .01, "b_err": .1, "c_err": 1., "chisquare": 1.}


def peak(channel=0, **updates):
    return {"channel": channel, "center": 500., "center_err": 1., "sigma": 20., "sigma_err": 1.,
            "amplitude": 1000., "amplitude_err": 30., "temp": 20., "bias": 28.5,
            "window_lo": 400., "window_hi": 600., "fit_mode": "gaus", "rsquared": .99,
            "fit_quality": {"accepted": True, "status": "resolved"}, **updates}


def seed_records(products, records):
    (products / "10B/tb/L0/parquet").mkdir(parents=True, exist_ok=True)
    for stem, rows in records.items():
        write_json(products / f"10B/tb/L1/json/{stem}_fit_results.l1.json", rows)


def collect(products):
    return TBL2Processor("10B", products)._collect_fit_data(
        products / "10B/tb/L1", products / "10B/tb/L0")


def test_legacy_rejection_and_low_r_squared_keep_a_real_peak_usable(tmp_path):
    legacy = peak(rsquared=.1, center_err=None,
                  fit_quality={"accepted": False, "status": "rejected", "redchi": 12.,
                               "warnings": ["structured residual"]})
    seed_records(tmp_path, {"legacy": [legacy]})
    rows = collect(tmp_path)
    assert len(rows) == 1
    assert rows[0]["gain"] == legacy["center"] and rows[0]["gain_err"] == 0.
    assert "structured residual" in rows[0]["quality_warnings"]
    assert any("R²" in reason for reason in rows[0]["quality_warnings"])
    assert usable_peak(legacy) is True


@pytest.mark.parametrize("updates", [
    {"fit_quality": {"accepted": False, "status": "fit_failed"}},
    {"placeholder": True}, {"fit_performed": False},
    {"fit_status": "not_fitted"},
    {"center": float("nan")}, {"center": float("inf")},
    {"center": None}, {"center": 0.}, {"center": True},
])
def test_unavailable_or_nonfinite_fits_do_not_become_warning_only_parameters(tmp_path, updates):
    record = peak(**updates)
    seed_records(tmp_path, {"unavailable": [record]})
    assert not usable_peak(record)
    assert collect(tmp_path) == []


def test_quality_warnings_keep_distinct_evidence_without_making_the_peak_unusable():
    record = peak(rsquared=.2, fit_quality={"accepted": False, "status": "rejected",
                                          "warnings": ["residual", "residual", None], "reason": "boundary"})
    reasons = quality_warnings(record, min_rsquared=.5)
    assert reasons.count("residual") == 1 and "boundary" in reasons
    assert any("R²" in reason for reason in reasons)
    assert usable_peak(record)


def test_tb_l2_continues_all_supported_channels_and_records_suspect_points(tmp_path, monkeypatch):
    products = tmp_path / "products"
    records = {f"case{index}": [
        peak(0, temp=float(index), rsquared=.15,
             fit_quality={"accepted": False, "status": "rejected", "warnings": ["counting residual"]}),
        peak(1, temp=float(index)), peak(2, temp=float(index)),
    ] for index in range(9)}
    records["failed"] = [peak(fit_quality={"accepted": False, "status": "fit_failed"})]
    seed_records(products, records)
    processor = TBL2Processor("10B", products)
    captured = {}

    def fit_surface(rows):
        channels = {row["channel"] for row in rows}
        assert len(channels) == 1
        captured[channels.pop()] = [row["file"] for row in rows]
        return {**PARAMS, "chi2": 1.}

    monkeypatch.setattr(processor, "_fit_surface", fit_surface)
    monkeypatch.setattr(processor, "_plot_channel_fit", lambda *_: None)
    result = processor.process(products / "10B/tb/L1", excluded_points=["10B:case8:0"])
    assert result["errors"] == []
    assert {channel: len(stems) for channel, stems in captured.items()} == {0: 8, 1: 9, 2: 9}
    assert "case8" not in captured[0] and "failed" not in captured[0]
    assert result["quality_policy"] == "warning_only"
    assert {point["id"] for point in result["suspect_points"]} == {f"10B:case{i}:0" for i in range(8)}
    assert result["warnings"]
    output = read_json(products / "10B/tb/L2/json" / result["output_json"])
    assert all(row["G0"] > 0 for row in output[:3]) and output[3]["G0"] == 0
    manifest = read_json(products / "10B/tb/L2/json/L2_manifest.json")
    assert manifest["suspect_points"] == result["suspect_points"]
    assert set(manifest["included_points"]) == {
        f"10B:{stem}:{ch}" for ch, stems in captured.items() for stem in stems
    }


def surface_fixture(tmp_path):
    root = tmp_path / "runs/review"
    products = root / "products"
    seed_records(products, {
        "suspect": [peak(rsquared=.1, fit_quality={"accepted": False, "status": "rejected"})],
        "good": [peak()], "masked": [peak()],
        "failed": [peak(fit_quality={"accepted": False, "status": "fit_failed"})],
        "placeholder": [peak(placeholder=True)], "nonfinite": [peak(center=float("nan"))],
    })
    write_json(products / "10B/tb/L2/json/2610251200_10B_TB.l2.json", [PARAMS] * 4)
    write_json(products / "10B/tb/L2/json/L2_manifest.json", {
        "quality_policy": "warning_only", "excluded_points": ["10B:masked:0"],
        "included_points": ["10B:suspect:0", "10B:good:0"],
    })
    return root, products, ComputeAdapter(tmp_path)


def test_surface_used_points_match_the_actual_aggregation_manifest(tmp_path):
    _root, products, adapter = surface_fixture(tmp_path)
    surface = adapter.surface("10B", products, 0)
    used = {point["id"] for point in surface["points"] if point.get("used")}
    collected = {f"10B:{row['file']}:{row['channel']}" for row in collect(products)}
    assert used == collected - {"10B:masked:0"} == {"10B:suspect:0", "10B:good:0"}
    assert surface["diagnostics"]["n_points"] == 2
    # Explicit snapshot membership overrides current eligibility after a refit
    # or a partial-import overlay changed another saved record.
    write_json(products / "10B/tb/L2/json/L2_manifest.json", {
        "quality_policy": "warning_only", "included_points": ["10B:good:0"], "excluded_points": []})
    surface = adapter.surface("10B", products, 0)
    assert {point["id"] for point in surface["points"] if point.get("used")} == {"10B:good:0"}


def test_current_manual_mask_changes_draft_without_rewriting_saved_membership(tmp_path):
    root, _products, adapter = surface_fixture(tmp_path)
    state = {"id": "review", "payload": "10B", "config": {"excluded": ["10B:good:0"]}, "dirty_layers": []}
    service = SimpleNamespace(_channel=Workbench._channel, read=lambda _: state,
                              root=lambda _: root, adapter_for=lambda _: adapter)
    result = Workbench.surface(service, "review", 0)
    points = {point["stem"]: point for point in result["points"]}
    assert points["suspect"]["used"] and points["suspect"]["draft_used"]
    assert points["good"]["actual_used"] and not points["good"]["used"] and not points["good"]["draft_used"]
    assert not points["masked"]["actual_used"] and points["masked"]["draft_used"]
    assert all(not points[stem]["draft_used"] for stem in ("failed", "placeholder", "nonfinite"))


@pytest.mark.parametrize("width,minimum", [(6., 48.), (2.5, 20.)])
def test_saved_bin_width_controls_display_and_minimum_fittable_window(tmp_path, width, minimum):
    products = tmp_path / "products"
    histogram = {"min": 0., "max": 1200., "bin_width": width}
    seed_records(products, {"spectrum": [peak(histogram_config=histogram)]})
    values = np.array([480., 490., 500., 510., 520.])
    write_parquet(products / "10B/tb/L0/parquet/spectrum.l0.parquet",
                  pd.DataFrame({"channel": 0, "amp": values, "crc_check": True}),
                  metadata={"temp": [20.] * 4, "bias": [28.5] * 4}, meta_key="hk_data",
                  schema_metadata={"acquisition": {"fitting": {"tb_histogram": {
                      "min": 0., "max": 4096., "bin_width": 1.}}}})
    spectrum = ComputeAdapter(tmp_path).spectrum("10B", products, "spectrum", 0)
    assert spectrum["histogram_config"] == histogram
    assert spectrum["window_bounds"]["minimum_width"] == minimum
    assert sum(spectrum["y"]) == len(values)
    np.testing.assert_allclose(np.diff(spectrum["x"]), width)
