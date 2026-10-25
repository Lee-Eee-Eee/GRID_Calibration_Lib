"""Resolved signal peaks beside dark noise, with original counting errors."""

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.peak_finding import PeakDetectionError, detect_photopeak
from calibration_lib.common.utils import read_json, write_json, write_parquet
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor
from calibration_lib.tb.processors.L1_processor import TBL1Processor
from calibration_lib.tb.processors.L2_processor import TBL2Processor


def adjacent_events(signal_counts=5000, seed=71):
    rng = np.random.default_rng(seed)
    return np.concatenate([
        rng.normal(28, 2, 15000), rng.exponential(14, 6000),
        rng.normal(48, 5, signal_counts), rng.uniform(0, 160, 500),
    ])


def histogram(events):
    counts, edges = np.histogram(events, np.arange(201))
    return (edges[1:] + edges[:-1]) / 2, counts


def test_adjacent_fainter_photopeak_is_selected_instead_of_tallest_dark_peak(tmp_path):
    events = adjacent_events()
    x, counts = histogram(events)
    assert 25 < x[np.argmax(counts)] < 31  # old window argmax locks here
    candidate, diagnostics = detect_photopeak(x, counts, window=[15, 80])
    assert abs(candidate.center - 48) < 2
    assert 30 < diagnostics["noise_boundary"] < 38
    assert any(row["noise_candidate"] for row in diagnostics["candidates"])
    fit = TBL1Processor("Custom", tmp_path)._fit_channel("sample", 0, events, {}, True)
    assert abs(fit.center - 48) < 2
    assert abs(fit.sigma - 5) < 1.5
    assert fit.fit_quality["accepted"]
    assert fit.lo > 30


def test_manual_window_is_kept_while_dark_noise_model_mismatch_is_annotated(tmp_path):
    windows = {"sample": {"0": {"lo": 15.125, "hi": 80.375}}}
    fit = TBL1Processor("Custom", tmp_path)._fit_channel("sample", 0, adjacent_events(), windows, True)
    assert fit.lo == 15.125 and fit.hi == 80.375
    assert abs(fit.center - 48) < 2
    assert fit.fit_quality["redchi"] > 3
    assert fit.fit_quality["accepted"] is False
    assert fit.fit_quality["status"] == "suspect"
    assert fit.fit_quality["suspect"] and fit.fit_quality["warning_only"]


def test_faint_unresolved_signal_never_falls_back_to_dark_peak():
    x, counts = histogram(adjacent_events(300))
    with pytest.raises(PeakDetectionError, match="No resolved photopeak") as error:
        detect_photopeak(x, counts)
    assert error.value.diagnostics["selected_center"] is None
    assert error.value.diagnostics["candidates"]
    assert any(row["noise_candidate"] for row in error.value.diagnostics["candidates"])


@pytest.mark.parametrize("kind", ["flat", "exponential"])
def test_continuum_counting_fluctuations_do_not_become_photopeaks(kind):
    rng = np.random.default_rng(123)
    x = np.arange(300) + 0.5
    expectation = np.full(300, 20.0) if kind == "flat" else 500 * np.exp(-x / 20) + 2
    with pytest.raises(PeakDetectionError):
        detect_photopeak(x, rng.poisson(expectation))


def test_competing_peaks_require_window_or_independent_anchor():
    rng = np.random.default_rng(22)
    x = np.arange(300) + 0.5
    counts = rng.poisson(2 + 100 * np.exp(-0.5 * ((x - 100) / 7)**2)
                         + 100 * np.exp(-0.5 * ((x - 180) / 7)**2))
    with pytest.raises(PeakDetectionError, match="Ambiguous") as error:
        detect_photopeak(x, counts, config={"infer_noise": False})
    assert error.value.diagnostics["selected_center"] is None
    assert len(error.value.diagnostics["candidates"]) >= 2
    selected, _ = detect_photopeak(x, counts, config={"infer_noise": False, "expected_center": 180})
    assert abs(selected.center - 180) < 2


def test_rate_scaling_retains_positions_significance_and_uncertainty_propagation():
    x, counts = histogram(adjacent_events())
    first, _ = detect_photopeak(x, counts, np.sqrt(np.maximum(counts, 1)))
    second, _ = detect_photopeak(x, counts / 120, np.sqrt(np.maximum(counts, 1)) / 120)
    assert first.center == second.center
    assert first.significance == pytest.approx(second.significance)
    assert first.sigma == pytest.approx(second.sigma)


def test_measured_noise_ceiling_is_used_without_fixed_payload_adc_floor():
    rng = np.random.default_rng(30)
    x, counts = histogram(rng.normal(48, 5, 3000))
    candidate, diagnostics = detect_photopeak(x, counts, config={"noise_ceiling": 25})
    assert candidate.center < 70
    assert diagnostics["noise_boundary"] == 25
    with pytest.raises(PeakDetectionError):
        detect_photopeak(x, counts, config={"noise_ceiling": 60})


def test_ec_uses_propagated_signal_and_background_errors_and_retains_manual_bounds(tmp_path):
    events = adjacent_events()
    rng = np.random.default_rng(311)
    background = rng.exponential(14, 5000)
    signal_df = pd.DataFrame({"amp_corr": events, "utc": np.linspace(0, 120, len(events))})
    background_df = pd.DataFrame({"amp_corr": background, "utc": np.linspace(0, 120, len(background))})
    fit, _ = ECL2Processor("Custom", tmp_path)._fit_energy_spectrum(
        "59.5keV", signal_df, background_df, 0, 1, 1,
        forced_window=[15.125, 80.375], forced_peak_energy=59.5,
    )
    assert fit["lo"] == 15.125 and fit["hi"] == 80.375
    assert abs(fit["center"] - 48) < 3
    assert fit["peak_detection"]["algorithm"] == "multiscale-snip-v2"
    assert fit["fit_quality"]["accepted"] is False


def test_saved_quality_warnings_are_retained_by_both_aggregators(tmp_path):
    good = {"channel": 0, "temp": 25, "bias": 28, "center": 48, "center_err": .2,
            "rsquared": .99, "fit_quality": {"accepted": False}}
    directory = tmp_path / "Custom/tb/L1/json"
    (tmp_path / "Custom/tb/L0/parquet").mkdir(parents=True)
    write_json(directory / "sample_fit_results.l1.json", [good])
    tb_rows = TBL2Processor("Custom", tmp_path)._collect_fit_data(tmp_path / "Custom/tb/L1", tmp_path / "Custom/tb/L0")
    assert len(tb_rows) == 1 and tb_rows[0]["quality_warnings"]
    write_json(directory / "sample_fit_results.l1.json", [{**good, "fit_quality": {"accepted": True}}])
    assert len(TBL2Processor("Custom", tmp_path)._collect_fit_data(tmp_path / "Custom/tb/L1", tmp_path / "Custom/tb/L0")) == 1
    ec_rows = ECL3Processor._filter_quality([{**good, "E": 59.5, "sigma": 5, "sigma_err": .2,
                                            "resolution": .2, "resolution_err": .01}])
    assert len(ec_rows) == 1 and ec_rows[0]["quality_warnings"]


@pytest.mark.parametrize("x,y", [([0, 1], [1, 2]), (np.arange(12), np.zeros(12)),
                                   (np.arange(12), np.r_[np.nan, np.ones(11)])])
def test_invalid_or_empty_spectra_report_a_reason(x, y):
    with pytest.raises(PeakDetectionError):
        detect_photopeak(np.asarray(x), np.asarray(y))


@pytest.mark.parametrize("key,value", [("min_significance", float("nan")),
                                       ("expected_center", float("inf")),
                                       ("expected_tolerance", 0), ("min_sigma_bins", -1),
                                       ("max_resolution", True), ("noise_ceiling", -1)])
def test_invalid_settings_never_leak_nonfinite_diagnostics(key, value):
    with pytest.raises(PeakDetectionError, match=key) as error:
        detect_photopeak(np.arange(20), np.ones(20), config={key: value})
    assert len(error.value.diagnostics["algorithm_sha256"]) == 64


def test_suspect_fit_can_supply_numeric_xray_consensus(tmp_path):
    rows = {ch: {"rsquared": .999, "center": 200 + ch, "center_err": .1,
                 "fit_quality": {"accepted": False}} for ch in range(4)}
    assert ECL2Processor("Custom", tmp_path)._xray_consensus_center(rows) == 201.5


def test_multiline_source_needs_energy_identity_anchor_and_scales_it_correctly(tmp_path, monkeypatch):
    rng = np.random.default_rng(702)
    signals, backgrounds = [], []
    for channel in range(4):
        events = np.concatenate([rng.normal(100, 7, 1600), rng.normal(250, 9, 6000),
                                 rng.uniform(0, 320, 300)])
        signals.append(pd.DataFrame({"amp_corr": events, "channel": channel,
                                     "utc": np.linspace(0, 120, len(events))}))
        bg = rng.uniform(0, 320, 500)
        backgrounds.append(pd.DataFrame({"amp_corr": bg, "channel": channel,
                                         "utc": np.linspace(0, 120, len(bg))}))
    folder = tmp_path / "Custom/ec/L1"
    write_parquet(folder / "parquet/src_Test_1m_corr.l1.parquet", pd.concat(signals),
                  metadata={"peak_energies_keV": [100, 250]}, meta_key="tb_correction")
    write_parquet(folder / "parquet/src_bkg_2m_corr.l1.parquet", pd.concat(backgrounds))
    fitter = ECL2Processor("Custom", tmp_path)
    monkeypatch.setattr(fitter, "_plot_energy_file", lambda *args: None)
    unresolved = fitter.process(folder, {"automatic_window": True})
    assert unresolved["n_processed"] == 0
    assert all("Multiple source lines require" in row["error"] for row in unresolved["errors"] if "channel" in row)
    resolved = fitter.process(folder, {"automatic_window": True,
                                      "peak_detection": {"expected_center": 250, "expected_energy_keV": 250}})
    assert not resolved["errors"], resolved["errors"]
    records = read_json(tmp_path / "Custom/ec/L2/json/src_Test_fit_results.l2.json")
    assert len(records) == 8
    assert all(abs(row["center"] - row["E"]) < 2 for row in records)
    assert all(row["fit_quality"]["accepted"] for row in records)
