"""Peak diagnostics annotate EC inputs while usable numbers keep flowing."""

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor
from calibration_lib.workbench.compute import ComputeAdapter
from calibration_lib.workbench.ec_interactive import ECInspector


def calibration_points():
    return [{"channel": channel, "E": energy, "center": energy * 6 + channel,
             "center_err": 1., "sigma": 5., "sigma_err": .2,
             "resolution": 12., "resolution_err": .5, "fwhm": 11.77,
             "source": "x", "rsquared": .99,
             "fit_quality": {"accepted": True}}
            for channel in range(4) for energy in (40., 45., 60., 80.)]


def test_suspect_parameters_are_used_by_real_ec_l3_and_inspector(tmp_path, monkeypatch):
    root = tmp_path / "products"
    points = calibration_points()
    suspect = points[0]
    suspect.update(rsquared=.1, center_err=60., sigma_err=20., resolution=99.)
    suspect["fit_quality"] = {"accepted": False, "status": "rejected", "warnings": ["structured residuals"]}
    path = root / "14B/ec/L2/json/fixture_fit_results.l2.json"
    write_json(path, points)
    previous = path.read_bytes()
    processor = ECL3Processor("14B", root)
    monkeypatch.setattr(processor, "_plot_ec_fit", lambda *_: None)
    monkeypatch.setattr(processor, "_plot_resolution_fit", lambda *_: None)
    result = processor.process(root / "14B/ec/L2")
    assert not result["errors"], result["errors"]
    assert result["quality_policy"] == "warning_only"
    assert any("structured residuals" in message for message in result["warnings"])
    assert all(value["EC_low"] is not None for value in result["calibration_params"].values())
    overview = ECInspector(ComputeAdapter(tmp_path)).overview("14B", root, 0)
    selected = next(row for row in overview["points"] if row["E"] == 40)
    assert selected["quality_pass"] and selected["quality_suspect"] and selected["used"]
    assert "structured residuals" in selected["quality_reasons"]
    assert overview["diagnostics"]["n_used"] == 4
    assert path.read_bytes() == previous


def test_nonmonotonic_point_is_retained_and_annotated():
    points = calibration_points()[:4]
    points[2]["center"] = points[1]["center"] * .8
    retained = ECL3Processor._filter_monotonic(ECL3Processor._filter_quality(points))
    assert retained == points
    assert "non-monotonic center" in points[2]["quality_warnings"]
    assert points[2]["quality_suspect"]


def test_nearby_adc_for_different_energy_lines_is_a_warning():
    points = calibration_points()[:4]
    points[2]["center"] = points[0]["center"] * 1.01
    retained = ECL3Processor._filter_adc_proximity(points)
    assert retained == points
    assert "ADC proximity of distinct energy lines" in points[0]["quality_warnings"]
    assert "ADC proximity of distinct energy lines" in points[2]["quality_warnings"]


@pytest.mark.parametrize("change", [{"center": None}, {"sigma": float("nan")},
                                   {"placeholder": True}, {"fit_quality": {"accepted": False, "status": "fit_failed"}}])
def test_invalid_parameters_and_unperformed_fits_are_not_numeric_inputs(change):
    point = {**calibration_points()[0], **change}
    assert not ECL3Processor.usable_record(point)
    assert ECL3Processor._filter_quality([point]) == []


@pytest.mark.parametrize("uncertainty", [None, 0., float("nan"), float("inf")])
def test_missing_uncertainty_uses_explicit_fallback_weights_and_keeps_source(tmp_path, monkeypatch, uncertainty):
    root = tmp_path / "products"
    points = calibration_points()
    for point in points:
        if point["channel"] == 0:
            point.update(center_err=uncertainty, sigma_err=uncertainty, resolution_err=uncertainty)
    path = root / "14B/ec/L2/json/fixture_fit_results.l2.json"
    write_json(path, points)
    previous = path.read_bytes()
    processor = ECL3Processor("14B", root)
    monkeypatch.setattr(processor, "_plot_ec_fit", lambda *_: None)
    monkeypatch.setattr(processor, "_plot_resolution_fit", lambda *_: None)
    result = processor.process(root / "14B/ec/L2")
    assert not result["errors"], result["errors"]
    assert result["calibration_params"]["0"]["EC_low"] is not None
    assert len(result["fit_weight_fallbacks"]) == 8
    assert all(item["source"] == "unit_weight" and item["effective_uncertainty"] == 1.
               for item in result["fit_weight_fallbacks"])
    assert path.read_bytes() == previous
    overview = ECInspector(ComputeAdapter(tmp_path)).overview("14B", root, 0)
    assert overview["diagnostics"]["n_used"] == 4
    assert all(row["quality_suspect"] and row["used"] for row in overview["points"])


def test_one_channel_numeric_failure_preserves_other_channels_and_output(tmp_path, monkeypatch):
    root = tmp_path / "products"
    write_json(root / "14B/ec/L2/json/fixture_fit_results.l2.json", calibration_points())
    processor = ECL3Processor("14B", root)
    original = processor._fit_channel
    called = []

    def fail_channel_zero(channel, *args):
        called.append(channel)
        if channel == 0:
            raise RuntimeError("numeric solver failed")
        return original(channel, *args)

    monkeypatch.setattr(processor, "_fit_channel", fail_channel_zero)
    monkeypatch.setattr(processor, "_plot_ec_fit", lambda *_: None)
    monkeypatch.setattr(processor, "_plot_resolution_fit", lambda *_: None)
    result = processor.process(root / "14B/ec/L2")
    assert called == [0, 1, 2, 3]
    assert result["calibration_params"]["0"]["EC_low"] is None
    assert all(result["calibration_params"][str(ch)]["EC_low"] for ch in (1, 2, 3))
    assert any("Channel 0: numeric solver failed" in error for error in result["errors"])
    assert result["output_json"]


def test_invalid_peak_center_does_not_hide_other_points_or_overview(tmp_path, monkeypatch):
    root = tmp_path / "products"
    points = calibration_points()
    points[0]["center"] = None
    write_json(root / "14B/ec/L2/json/fixture_fit_results.l2.json", points)
    processor = ECL3Processor("14B", root)
    monkeypatch.setattr(processor, "_plot_ec_fit", lambda *_: None)
    monkeypatch.setattr(processor, "_plot_resolution_fit", lambda *_: None)
    result = processor.process(root / "14B/ec/L2")
    assert not result["errors"], result["errors"]
    overview = ECInspector(ComputeAdapter(tmp_path)).overview("14B", root, 0)
    assert overview["diagnostics"]["n_used"] == 3
    bad = next(row for row in overview["points"] if row["E"] == 40.)
    assert not bad["used"] and not bad["quality_pass"]
    assert bad["predicted_energy"] is None
    assert all(value is not None for value in overview["curve"]["x"])


def test_custom_bin_width_controls_background_selection_and_real_net_spectrum(tmp_path, monkeypatch):
    root = tmp_path / "products"
    rng = np.random.default_rng(2012)
    for channel in (0, 1):
        amp = np.r_[rng.normal(400. + 150 * channel, 20., 4000), rng.uniform(150, 800, 400)]
        frame = pd.DataFrame({"channel": channel, "amp_corr": amp, "utc": np.linspace(0, 60, len(amp))})
        write_parquet(root / f"Test/ec/L1/parquet/40keV_ch{channel}_corr.l1.parquet", frame,
                      metadata={"energy_keV": 40.}, meta_key="tb_correction")
    processor = ECL2Processor("Test", root)
    original = processor._build_rate_spectrum
    widths = []

    def capture(amplitudes, times, edges):
        widths.append(np.diff(edges))
        return original(amplitudes, times, edges)

    monkeypatch.setattr(processor, "_build_rate_spectrum", capture)
    monkeypatch.setattr(processor, "_plot_energy_file", lambda *_: None)
    point = "Test:ec:40keV:0:40"
    result = processor.process(root / "Test/ec/L1", {"only_point": point, "bin_width": 8.,
                               "ec_windows": {point: {"lo": 330., "hi": 470.}},
                               "peak_detection": {"infer_noise": False}})
    assert not result["errors"], result["errors"]
    assert len(widths) == 4  # Signal/background for selection, then for the fit.
    assert all(np.allclose(width, 8.) for width in widths)
    record = read_json(root / "Test/ec/L2/json/40keV_fit_results.l2.json")[0]
    assert record["bin_width"] == 8.
    spectrum = ECInspector(ComputeAdapter(tmp_path)).spectrum("Test", root, point)
    np.testing.assert_allclose(np.diff(spectrum["x"]), 8.)
    assert spectrum["window_bounds"]["minimum_width"] == 64.


@pytest.mark.parametrize("width", [0., -2., float("nan"), float("inf")])
def test_invalid_bin_width_is_reported_before_processing(tmp_path, width):
    with pytest.raises(ValueError, match="bin_width"):
        ECL2Processor("Test", tmp_path).process(tmp_path, {"bin_width": width})
