"""Independent line-in-continuum cases for wide-scale TB peak review.

Synthetic line identities below are defined by generation, never by a legacy
fit window or by whichever candidate the production algorithm selects.
"""

import numpy as np
import pytest
from calibration_lib.common.peak_finding import PeakDetectionError, propose_photopeaks
from calibration_lib.tb.processors.L1_processor import TBL1Processor
from scipy.special import ndtr


def gaussian_counts(edges, center, sigma, area):
    return area * np.diff(ndtr((edges - center) / sigma))


def wide_spectrum(width=6):
    edges = np.arange(0, 9000 + width, width, dtype=float)
    x = (edges[:-1] + edges[1:]) / 2
    background = width * (0.03 + 8 * np.exp(-x / 1000))
    counts = (background + gaussian_counts(edges, 4200, 280, 80000)
              + gaussian_counts(edges, 150, 9, 30000))
    return x, counts


def fit_histogram(processor, x, counts):
    return processor._fit_histogram("synthetic", 0, x, counts, {}, True)


def assert_no_false_acceptance(processor, x, counts, center=None, tolerance=None):
    try:
        fit = fit_histogram(processor, x, counts)
    except PeakDetectionError:
        return
    if fit.fit_quality["accepted"]:
        assert center is not None, (fit.center, fit.fit_quality)
        assert abs(fit.center - center) < tolerance, (fit.center, fit.fit_quality)


def test_broad_high_gain_line_survives_strong_dark_component_and_curved_continuum(tmp_path):
    x, expectation = wide_spectrum()
    counts = np.random.default_rng(812).poisson(expectation)
    fit = fit_histogram(TBL1Processor("Synthetic", tmp_path), x, counts)
    assert abs(fit.center - 4200) < 35
    assert abs(fit.sigma - 280) < 45
    assert fit.fit_quality["accepted"], fit.fit_quality


def test_candidate_bank_retains_intrinsic_width_of_a_broad_line():
    x, counts = wide_spectrum()
    candidates, _ = propose_photopeaks(x, counts, include_noise=True)
    signal = min(candidates, key=lambda candidate: abs(candidate.center - 4200))
    assert abs(signal.center - 4200) < 35
    assert 180 < signal.sigma < 380
    assert signal.scales >= 2


def test_broad_peak_positions_and_widths_do_not_depend_on_count_rate_units():
    x, counts = wide_spectrum()
    errors = np.sqrt(np.maximum(counts, 1))
    count_candidates, _ = propose_photopeaks(x, counts, errors, include_noise=True)
    rate_candidates, _ = propose_photopeaks(x, counts / 73, errors / 73, include_noise=True)
    count_signal = min(count_candidates, key=lambda candidate: abs(candidate.center - 4200))
    rate_signal = min(rate_candidates, key=lambda candidate: abs(candidate.center - 4200))
    assert rate_signal.center == pytest.approx(count_signal.center)
    assert rate_signal.sigma == pytest.approx(count_signal.sigma)
    assert rate_signal.shape_significance == pytest.approx(count_signal.shape_significance)


@pytest.mark.parametrize("width", [2, 4, 8, 16])
def test_rebinning_the_same_integrated_spectrum_keeps_the_broad_line(width):
    x, counts = wide_spectrum(width)
    candidates, _ = propose_photopeaks(x, counts, include_noise=True)
    signal = min(candidates, key=lambda candidate: abs(candidate.center - 4200))
    assert abs(signal.center - 4200) < max(30, 2 * width)
    assert 180 < signal.sigma < 380


def test_neighboring_dark_peak_cannot_redirect_acceptance_to_a_weak_remote_hump(tmp_path):
    edges = np.arange(0, 1402, 2, dtype=float)
    x = (edges[:-1] + edges[1:]) / 2
    counts = (2 * (0.6 + 70 * np.exp(-x / 95))
              + gaussian_counts(edges, 170, 11, 60000)
              + gaussian_counts(edges, 290, 18, 18000)
              + gaussian_counts(edges, 735, 38, 700))
    counts = np.random.default_rng(954).poisson(counts)
    # An inadequate single-background fit may be rejected. It must not turn
    # the separately generated weak high-side feature into the chosen line.
    assert_no_false_acceptance(TBL1Processor("Synthetic", tmp_path), x, counts,
                              center=290, tolerance=25)


@pytest.mark.parametrize("kind", ["exponential", "quadratic", "broad_hump"])
def test_smooth_background_has_no_accepted_gaussian_line(tmp_path, kind):
    x = np.arange(0, 1200, 2, dtype=float) + 1
    if kind == "exponential":
        counts = 800 * np.exp(-x / 180) + 8
    elif kind == "quadratic":
        counts = 20 + .0002 * (x - 1100) ** 2
    else:
        counts = 20 + 180 * np.exp(-.5 * ((x - 400) / 380) ** 2)
    assert_no_false_acceptance(TBL1Processor("Synthetic", tmp_path), x, counts)
