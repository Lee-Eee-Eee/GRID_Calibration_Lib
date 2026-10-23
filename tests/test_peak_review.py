"""Independent regressions for background evidence and propagated variances."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from calibration_lib.common.peak_finding import (
    PeakDetectionError,
    detect_photopeak,
)
from calibration_lib.ec.processors.L2_processor import ECL2Processor
from calibration_lib.ec.processors.L3_processor import ECL3Processor


def test_exponential_fluctuation_is_rejected_before_peak_fitting():
    """A curved continuum must not be accepted as a photopeak."""
    x = np.arange(300) + 0.5
    y = np.random.default_rng(12).poisson(500 * np.exp(-x / 20) + 2)
    with pytest.raises(PeakDetectionError, match="No resolved photopeak") as error:
        detect_photopeak(x, y)
    json.dumps(error.value.diagnostics, allow_nan=False)


def test_ec_keeps_per_bin_propagated_errors_instead_of_a_median_floor(tmp_path, monkeypatch):
    rng = np.random.default_rng(86)
    signal = np.r_[rng.uniform(0, 220, 6000), rng.normal(120, 6, 5000)]
    background = rng.uniform(0, 220, 6000)
    signal_df = pd.DataFrame({"amp_corr": signal, "utc": np.linspace(0, 60, len(signal))})
    background_df = pd.DataFrame({"amp_corr": background,
                                  "utc": np.linspace(0, 120, len(background))})
    processor = ECL2Processor("Review", tmp_path)
    prepared = processor._prepare_spectrum(signal_df, background_df, 1)
    original = processor._select_best_fit
    seen = {}

    def capture(x, y, err, *args, **kwargs):
        seen["x"] = x.copy()
        seen["err"] = err.copy()
        return original(x, y, err, *args, **kwargs)

    monkeypatch.setattr(processor, "_select_best_fit", capture)
    processor._fit_energy_spectrum(
        "59.5keV", signal_df, background_df, 0, 1, 1,
        forced_window=[97.125, 142.875], forced_peak_energy=59.5,
        peak_config={"infer_noise": False},
    )
    mask = np.isin(prepared["mids"], seen["x"])
    assert np.ptp(prepared["net_err"][mask]) > 0.1
    np.testing.assert_array_equal(seen["err"], prepared["net_err"][mask])


@pytest.mark.parametrize("key,value", [
    ("expected_center", float("nan")),
    ("expected_center", float("inf")),
    ("min_significance", float("nan")),
    ("min_significance", 0),
    ("expected_tolerance", float("nan")),
    ("expected_tolerance", 0),
    ("min_sigma_bins", -1),
])
def test_peak_settings_reject_nonfinite_or_nonphysical_values(key, value):
    x = np.arange(200) + 0.5
    y = np.random.default_rng(4).poisson(2 + 80 * np.exp(-0.5 * ((x - 100) / 7) ** 2))
    with pytest.raises(PeakDetectionError):
        detect_photopeak(x, y, config={"infer_noise": False, key: value})


def test_narrow_line_can_be_used_when_detector_noise_inference_is_disabled():
    """A high-resolution detector line must not inherit the SiPM heuristic."""
    rng = np.random.default_rng(1234)
    events = np.r_[rng.normal(35, 1.5, 8000), rng.exponential(65, 6000)]
    y, edges = np.histogram(events, np.arange(501))
    candidate, diagnostics = detect_photopeak(
        (edges[:-1] + edges[1:]) / 2, y, config={"infer_noise": False},
    )
    assert abs(candidate.center - 35) < 1
    assert not candidate.noise_candidate
    assert diagnostics["noise_boundary"] is None


def test_legacy_channel_dictionary_retains_explicit_quality_rejection(tmp_path):
    root = tmp_path / "ec/L2"
    (root / "json").mkdir(parents=True)
    rejected = {
        "E": 59.5, "center": 120, "center_err": 0.2,
        "sigma": 6, "sigma_err": 0.1,
        "resolution": 11.77, "resolution_err": 0.2,
        "FWHM": 14.13, "rsquared": 0.99,
        "fit_quality": {"accepted": False, "status": "rejected"},
    }
    (root / "json/59.5keV_l2_fit_results.json").write_text(
        json.dumps({"energy": "59.5keV", "channels": {"0": rejected}}),
        encoding="utf8",
    )
    processor = ECL3Processor("Review", tmp_path)
    rows = processor._collect_l2_results(root)
    assert len(rows) == 1
    assert rows[0]["fit_quality"]["accepted"] is False
    assert processor._filter_quality(rows) == []


def test_actual_14b_weak_photopeak_never_silently_locks_to_the_dark_component():
    fixture = json.loads((Path(__file__).parent / "fixtures/14B_30_283_075_ch2_histogram.json")
                         .read_text(encoding="utf8"))
    y = np.asarray(fixture["counts"], dtype=float)
    x = fixture["bin_lo"] + (np.arange(len(y)) + 0.5) * fixture["bin_width"]
    # Wider scales now resolve the weak signal; neither success nor ambiguity
    # may return the large dark component as the calibration peak.
    candidate, diagnostics = detect_photopeak(x, y)
    assert 190 < candidate.center < 220
    assert any(abs(row["center"] - 84.5) < 2 for row in diagnostics["candidates"])
    assert any(190 < row["center"] < 220 for row in diagnostics["candidates"])
    # An independent center makes the intended signal candidate identifiable;
    # subsequent model quality still decides whether it can enter calibration.
    candidate, _ = detect_photopeak(x, y, config={"expected_center": 200})
    assert 190 < candidate.center < 220
