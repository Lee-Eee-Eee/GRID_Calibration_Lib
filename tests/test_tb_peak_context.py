"""Independent physical-grid checks for leave-one-spectrum-out search guides."""

import numpy as np
import pytest
from calibration_lib.common.tb_peak_context import gain_guides


def synthetic_series():
    temperature, bias = np.meshgrid([-12., 7., 27., 57.], [26.5, 27., 27.5, 28., 28.5, 29.], indexing="ij")
    temperature, bias = temperature.ravel(), bias.ravel()
    x = np.arange(3., 9000., 6.)
    sqrt_center = 10 * (bias - 23.5 - .015 * (temperature - 10))
    centers = sqrt_center ** 2
    spectra = []
    for center in centers:
        sigma = .045 * center
        spectra.append(3 + 30 * np.exp(-x / 900)
                       + 2500 * np.exp(-.5 * ((x - 150) / 10) ** 2)
                       + 600 * np.exp(-.5 * ((x - center) / sigma) ** 2)
                       + 12 * np.exp(-.5 * ((x - 2.5 * center) / (.1 * center)) ** 2))
    return x, np.asarray(spectra), temperature, bias, centers


def test_physical_grid_guides_exclude_each_target_and_follow_the_generated_gain():
    x, spectra, temperature, bias, centers = synthetic_series()
    guides = gain_guides(x, spectra, temperature, bias)
    assert len(guides) == 24
    for target, (guide, center) in enumerate(zip(guides, centers, strict=True)):
        assert guide is not None
        assert guide["target_excluded"] is True
        assert target not in guide["anchor_indices"]
        assert len(set(guide["anchor_indices"])) == len(guide["anchor_indices"]) >= 12
        assert guide["expected_center"] == pytest.approx(center, rel=.035)


def test_destroying_or_replacing_target_counts_cannot_change_its_own_guide():
    x, spectra, temperature, bias, _ = synthetic_series()
    target = 10
    original = gain_guides(x, spectra, temperature, bias)[target]
    assert original is not None
    for replacement in (np.zeros_like(x), 8000 * np.exp(-.5 * ((x - 7800) / 120) ** 2)):
        modified = spectra.copy()
        modified[target] = replacement
        guide = gain_guides(x, modified, temperature, bias)[target]
        assert guide is not None
        assert guide["anchor_indices"] == original["anchor_indices"]
        assert guide["expected_center"] == pytest.approx(original["expected_center"], abs=1e-10)
        assert guide["expected_tolerance"] == pytest.approx(original["expected_tolerance"], abs=1e-12)


@pytest.mark.parametrize("kind", ["too_few", "single_temperature", "single_bias", "short_temperature_span", "short_bias_span"])
def test_insufficient_condition_coverage_does_not_invent_a_gain_guide(kind):
    x, spectra, temperature, bias, _ = synthetic_series()
    if kind == "too_few":
        spectra, temperature, bias = spectra[:12], temperature[:12], bias[:12]
    elif kind == "single_temperature":
        temperature[:] = 25
    elif kind == "single_bias":
        bias[:] = 28
    elif kind == "short_temperature_span":
        temperature = 25 + temperature / 100
    else:
        bias = 28 + (bias - 28) / 100
    assert gain_guides(x, spectra, temperature, bias) == [None] * len(spectra)


@pytest.mark.parametrize("kind", ["counting_background", "stationary_dark_component"])
def test_background_or_stationary_noise_cannot_define_a_gain_trajectory(kind):
    x, spectra, temperature, bias, _ = synthetic_series()
    if kind == "counting_background":
        spectra = np.random.default_rng(772).poisson(4 + 40 * np.exp(-x / 150), size=spectra.shape)
    else:
        spectrum = 4 + 30 * np.exp(-x / 200) + 1000 * np.exp(-.5 * ((x - 150) / 10) ** 2)
        spectra = np.tile(spectrum, (len(spectra), 1))
    assert gain_guides(x, spectra, temperature, bias) == [None] * len(spectra)
