"""Statistics-aware photopeak candidates, shared by the TB and EC fitters.

The continuum estimate is used only to locate peaks. Fits always use the original
histogram and its counting errors. A low narrow component is a *noise candidate*,
not an identification of a particular SiPM noise process.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from lmfit.models import ExponentialModel
from scipy.ndimage import gaussian_filter1d
from scipy.signal import find_peaks

ALGORITHM = "multiscale-snip-v1"
ALGORITHM_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class PeakDetectionError(RuntimeError):
    """No unique, statistically resolved peak can be recommended."""

    def __init__(self, message: str, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {"algorithm": ALGORITHM, "candidates": [],
                                            "noise_boundary": None, "warnings": [message]}
        self.diagnostics["algorithm_sha256"] = ALGORITHM_SHA256


@dataclass
class PeakCandidate:
    center: float
    sigma: float
    significance: float
    prominence: float
    window: list[float]
    scales: int
    noise_candidate: bool = False
    score: float = 0.0


def _snip(y: np.ndarray, radius: int) -> np.ndarray:
    """Decreasing-window SNIP in the log-log-square-root domain."""
    transformed = np.log1p(np.log1p(np.sqrt(np.maximum(y, 0.0) + 1.0)))
    baseline = transformed.copy()
    for half in range(min(radius, (len(y) - 1) // 2), 0, -1):
        baseline[half:-half] = np.minimum(
            baseline[half:-half], 0.5 * (baseline[:-2 * half] + baseline[2 * half:]),
        )
    return np.maximum(np.expm1(np.expm1(baseline)) ** 2 - 1.0, 0.0)


def detect_photopeak(
    x: np.ndarray,
    y: np.ndarray,
    errors: np.ndarray | None = None,
    *,
    window: tuple[float, float] | list[float] | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[PeakCandidate, dict[str, Any]]:
    """Return a resolved peak and serializable evidence for the recommendation.

    ``noise_ceiling`` is an optional measured threshold in the same ADC units as
    x. Without it a leading, narrower, dominant low-amplitude component can be
    flagged empirically. No fixed ADC cutoff is shared across payloads. Expected
    centers must come from an independent calibration/line ratio, never keV=ADC.
    """
    settings = config or {}
    for key, default in (("min_significance", 4.5), ("min_sigma_bins", 1.0),
                         ("max_resolution", 1.2), ("expected_tolerance", 0.25)):
        value = settings.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value) or value <= 0:
            raise PeakDetectionError(f"{key} must be finite and positive")
    for key in ("noise_ceiling", "expected_center", "expected_energy_keV"):
        value = settings.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not np.isfinite(value) or value < 0
                                  or key != "noise_ceiling" and value == 0):
            raise PeakDetectionError(f"{key} must be a finite {'nonnegative' if key == 'noise_ceiling' else 'positive'} value")
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if x.ndim != 1 or y.shape != x.shape or x.size < 9:
        raise PeakDetectionError("No resolved photopeak: histogram has fewer than nine bins")
    if not np.isfinite(x).all() or not np.isfinite(y).all() or np.any(np.diff(x) <= 0):
        raise PeakDetectionError("No resolved photopeak: histogram contains invalid coordinates/counts")
    dx = float(np.median(np.diff(x)))
    if not np.allclose(np.diff(x), dx, rtol=1e-3, atol=1e-8):
        raise PeakDetectionError("Peak detection requires uniformly spaced histogram bins")
    err = np.sqrt(np.maximum(y, 1.0)) if errors is None else np.asarray(errors, dtype=float)
    if err.shape != y.shape or not np.isfinite(err).all() or np.any(err < 0):
        raise PeakDetectionError("Invalid counting uncertainties for peak detection")
    positive_errors = err[err > 0]
    floor = float(np.median(positive_errors)) if positive_errors.size else 1.0
    err = np.maximum(err, max(floor * 0.1, np.finfo(float).eps))
    if not np.any(y > 0):
        raise PeakDetectionError("No resolved photopeak: empty or nonpositive net spectrum")
    lower, upper = (float(x[0]), float(x[-1])) if window is None else tuple(map(float, window))
    if not np.isfinite([lower, upper]).all() or upper <= lower:
        raise PeakDetectionError("Invalid peak search window")
    minimum = float(settings.get("min_significance", 4.5))
    min_sigma = float(settings.get("min_sigma_bins", 1.0)) * dx
    # SNIP is scale normalized so count-rate and count histograms have identical
    # peak positions. Subtracted EC spectra retain their propagated uncertainties.
    norm = max(floor, np.finfo(float).eps)
    continuum = _snip(np.maximum(y, 0.0) / norm, min(80, max(12, x.size // 8))) * norm
    residual = y - continuum
    proposed: list[PeakCandidate] = []
    for scale in (1.0, 2.0, 4.0):
        smooth = gaussian_filter1d(residual, scale, mode="nearest")
        # Gaussian smoothing propagates independent-bin variance using k**2.
        radius = max(2, int(np.ceil(4 * scale)))
        grid = np.arange(-radius, radius + 1, dtype=float)
        kernel = np.exp(-0.5 * (grid / scale) ** 2)
        kernel /= kernel.sum()
        variance = np.convolve(err**2, kernel**2, mode="same")
        indices, props = find_peaks(smooth, prominence=(None, None), width=(None, None))
        for i, index in enumerate(indices):
            local_noise = max(float(np.sqrt(variance[index])), np.finfo(float).eps)
            prominence = float(props["prominences"][i])
            if prominence < 2.5 * local_noise:
                continue
            width = float(props["widths"][i]) * dx / 2.354820045
            sigma = float(np.sqrt(max(width**2 - (scale * dx)**2, (0.8 * dx)**2)))
            if sigma < min_sigma or sigma > float(settings.get("max_resolution", 1.2)) * max(x[index], dx) / 2.354820045:
                continue
            core = np.abs(x - x[index]) <= 2.0 * sigma
            side = (np.abs(x - x[index]) >= 2.5 * sigma) & (np.abs(x - x[index]) <= 4.5 * sigma)
            if np.count_nonzero(side) < 4 or np.count_nonzero(core) < 3:
                continue
            # SNIP is downward biased in empty/low-count bins. Significance
            # therefore uses an independent local sideband and propagates its
            # estimated level/slope variance as well as the core counting noise.
            basis = np.column_stack([np.ones(len(x)), (x - x[index]) / sigma])
            weighted = basis[side] / err[side, None]
            covariance = np.linalg.pinv(weighted.T @ weighted)
            coefficients = covariance @ (weighted.T @ (y[side] / err[side]))
            local_background = basis[core] @ coefficients
            area = float(np.sum(y[core] - local_background))
            summed_basis = basis[core].sum(axis=0)
            area_variance = float(np.sum(err[core] ** 2) + summed_basis @ covariance @ summed_basis)
            significance = area / max(float(np.sqrt(area_variance)), np.finfo(float).eps)
            if significance < minimum:
                continue
            match = next((c for c in proposed if abs(c.center - x[index]) < max(3 * dx, 1.25 * c.sigma)), None)
            if match is not None:
                match.scales += 1
                continue
            proposed.append(PeakCandidate(
                center=float(x[index]), sigma=sigma, significance=significance,
                prominence=prominence,
                window=[max(float(x[0]), float(x[index] - 3.5 * sigma)),
                        min(float(x[-1]), float(x[index] + 3.5 * sigma))], scales=1,
            ))
    proposed.sort(key=lambda candidate: candidate.center)
    stable = [candidate for candidate in proposed if candidate.scales >= 2]
    # A measured noise ceiling takes precedence. A heuristic leading component
    # is only excluded when a separate, broader and lower-amplitude peak exists.
    noise_boundary = settings.get("noise_ceiling")
    warnings: list[str] = []
    if noise_boundary is not None:
        noise_boundary = float(noise_boundary)
        if not np.isfinite(noise_boundary):
            raise PeakDetectionError("noise_ceiling must be finite ADC units")
        for candidate in stable:
            candidate.noise_candidate = candidate.center <= noise_boundary
    elif len(stable) >= 2 and settings.get("infer_noise", True):
        leading, following = stable[0], stable[1]
        if (leading.center < x[0] + 0.8 * (following.center - x[0])
                and leading.sigma < 0.8 * following.sigma
                and leading.prominence > 1.5 * following.prominence
                and following.center - leading.center > 3 * leading.sigma):
            leading.noise_candidate = True
            noise_boundary = float(leading.center + 2.5 * leading.sigma)
            warnings.append("Leading narrow component excluded as a noise candidate; verify with a dark/background measurement")
    if noise_boundary is None and stable and settings.get("infer_noise", True):
        leading = stable[0]
        cumulative = np.cumsum(np.maximum(y, 0.0))
        support = float(x[min(int(np.searchsorted(cumulative, 0.995 * cumulative[-1])), len(x) - 1)])
        # A narrow leading component with a long populated upper tail is not
        # enough to identify a scintillation photopeak. This protects the case
        # where the nearby signal itself is below the detection threshold.
        if ((window is None or leading.center < lower + 0.35 * (upper - lower))
                and leading.center < x[0] + 0.4 * (support - x[0])
                and 2.354820045 * leading.sigma / max(leading.center - x[0], dx) < 0.25):
            leading.noise_candidate = True
            noise_boundary = float(leading.center + 2.5 * leading.sigma)
            warnings.append("Narrow leading component has unresolved noise identity; supply a measured noise threshold or an independent peak anchor")
    expected = settings.get("expected_center")
    tolerance = float(settings.get("expected_tolerance", 0.25))
    if expected is not None and settings.get("noise_ceiling") is None:
        for candidate in stable:
            if abs(candidate.center - float(expected)) < max(candidate.sigma, 0.1 * abs(float(expected))):
                candidate.noise_candidate = False
        if not any(candidate.noise_candidate for candidate in stable):
            noise_boundary = None
    candidates = []
    eligible = list(stable)
    if window is not None or expected is not None:
        eligible.extend(candidate for candidate in proposed if candidate.scales < 2
                        and candidate.significance >= minimum * 1.1)
    for candidate in eligible:
        if candidate.noise_candidate or not lower <= candidate.center <= upper:
            continue
        if candidate.center - lower < dx or upper - candidate.center < dx:
            continue
        candidate.score = candidate.significance
        if expected is not None:
            distance = abs(candidate.center - float(expected)) / max(abs(float(expected)) * tolerance, 2 * dx)
            if distance > 1.5:
                continue
            candidate.score /= 1.0 + distance**2
        candidates.append(candidate)
    if not candidates:
        raise PeakDetectionError(
            "No resolved photopeak above counting noise in the selected window; "
            "check the noise threshold, widen the signal side, or acquire more counts",
            {"algorithm": ALGORITHM, "manual_window": window is not None,
             "noise_boundary": noise_boundary, "candidates": [asdict(c) for c in proposed],
             "warnings": warnings, "selected_center": None, "min_significance": minimum},
        )
    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    selected = candidates[0]
    if selected.scales < 2:
        warnings.append("Single-scale candidate admitted by the explicit window/independent center; inspect its fitted uncertainty")
    if (window is None and expected is None and settings.get("infer_noise", True)
            and selected is proposed[0]
            and any(candidate.center > selected.center + 4 * selected.sigma
                    and selected.center < x[0] + 0.55 * (candidate.center - x[0])
                    and candidate.scales < 2 and candidate.significance > minimum * 1.1
                    for candidate in proposed)):
        raise PeakDetectionError(
            "Leading peak has an unresolved higher-ADC competitor; it may be dark noise. "
            "Provide a measured noise ceiling or an independently calibrated expected center",
            {"algorithm": ALGORITHM, "manual_window": False, "noise_boundary": noise_boundary,
             "candidates": [asdict(candidate) for candidate in proposed], "selected_center": None,
             "warnings": [*warnings, "Signal identity unresolved across smoothing scales"],
             "min_significance": minimum},
        )
    if len(candidates) > 1:
        other = candidates[1]
        if other.score > 0.82 * selected.score and abs(other.center - selected.center) > 2 * max(other.sigma, selected.sigma):
            raise PeakDetectionError(
                f"Ambiguous photopeak candidates at {selected.center:g} and {other.center:g} ADC; "
                "narrow the window or provide an independently calibrated expected center",
                {"algorithm": ALGORITHM, "manual_window": window is not None,
                 "noise_boundary": noise_boundary, "candidates": [asdict(c) for c in proposed],
                 "warnings": [*warnings, "Multiple candidates have comparable statistical support"],
                 "selected_center": None, "min_significance": minimum},
            )
        warnings.append("Multiple resolved candidates; inspect the selected peak and alternatives")
    if noise_boundary is not None and selected.center - 2 * selected.sigma < noise_boundary:
        warnings.append("Photopeak overlaps the estimated noise region; inspect background residuals")
    if selected.center - lower < 2 * selected.sigma or upper - selected.center < 2 * selected.sigma:
        warnings.append("Selected window truncates the peak; center/width may be biased")
    selected.window = [max(lower, selected.window[0]), min(upper, selected.window[1])]
    diagnostics = {
        "algorithm": ALGORITHM, "algorithm_sha256": ALGORITHM_SHA256, "manual_window": window is not None,
        "noise_boundary": noise_boundary, "selected_center": selected.center,
        "candidates": [asdict(candidate) for candidate in proposed], "warnings": warnings,
        "min_significance": minimum,
    }
    return selected, diagnostics


def fit_quality(result: Any, x: np.ndarray, y: np.ndarray, errors: np.ndarray) -> dict[str, Any]:
    """Check signal-vs-continuum evidence, boundaries and uncertainty, beyond R²."""
    center, sigma = result.params["g_center"], result.params["g_sigma"]
    amplitude = result.params["g_amplitude"]
    weights = 1.0 / np.maximum(np.asarray(errors, dtype=float), np.finfo(float).eps)
    # Same-window weighted background-only competitor. Linear/quadratic are
    # solved analytically; exponential uses the existing lmfit component.
    residual_chi = float(np.sum(((y - result.best_fit) * weights) ** 2))
    background_chi = float(np.sum((y * weights) ** 2))
    background_k = 0
    for degree in (1, 2):
        design = np.polynomial.polynomial.polyvander((x - x.mean()) / max(float(np.ptp(x)), 1.0), degree)
        coefficients = np.linalg.lstsq(design * weights[:, None], y * weights, rcond=None)[0]
        chi = float(np.sum(((y - design @ coefficients) * weights) ** 2))
        if chi + (degree + 1) * np.log(len(x)) < background_chi + background_k * np.log(len(x)):
            background_chi, background_k = chi, degree + 1
    try:
        exponential = ExponentialModel()
        parameters = exponential.make_params(amplitude=max(float(np.max(y)), 1e-9),
                                             decay=max(float(np.ptp(x)), 1.0))
        parameters["amplitude"].set(min=0)
        parameters["decay"].set(min=max(float(np.median(np.diff(x))), 1e-6))
        background_result = exponential.fit(y, parameters, x=x - x[0], weights=weights)
        chi = float(np.sum(((y - background_result.best_fit) * weights) ** 2))
        if background_result.success and chi + 2 * np.log(len(x)) < background_chi + background_k * np.log(len(x)):
            background_chi, background_k = chi, 2
    except (ValueError, RuntimeError, FloatingPointError):
        pass
    # Known counting variance => χ² + k log n (not lmfit's unknown-variance BIC).
    delta_bic = background_chi - residual_chi + (background_k - result.nvarys) * np.log(len(x))
    snr = float(amplitude.value / amplitude.stderr) if amplitude.stderr and amplitude.stderr > 0 else None
    near_edge = min(center.value - center.min, center.max - center.value) < 0.05 * max(sigma.value, 1e-12)
    warnings = []
    if delta_bic < 6:
        warnings.append("Peak model is weakly supported over a smooth background (delta BIC < 6)")
    if snr is None or snr < 3:
        warnings.append("Peak area uncertainty is unavailable or area SNR < 3")
    if near_edge:
        warnings.append("Fitted center reaches its candidate boundary")
    if sigma.stderr is None or sigma.stderr > sigma.value:
        warnings.append("Peak width is poorly constrained")
    if result.redchi > 3:
        warnings.append("Counting residuals exceed the single-peak/background model (reduced chi-square > 3)")
    return {"delta_bic_background": float(delta_bic), "amplitude_snr": snr,
            "redchi": float(result.redchi), "center_at_boundary": bool(near_edge),
            "warnings": warnings, "accepted": not warnings,
            "status": "rejected" if warnings else "resolved"}
