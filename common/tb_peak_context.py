"""Leave-one-spectrum-out search guides for a monochromatic TB series.

Guides constrain line identity, never final peak values or uncertainties. They
use L0 counts and measured HK, without TBL2 results or historical hand windows.
The target spectrum is excluded from its own guide.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares

from .peak_finding import PeakDetectionError, propose_photopeaks


def gain_guides(x, counts, temperatures, biases, settings=None):
    """Infer soft center priors from a TB series; None means no usable guide.

    sqrt(gain) is approximately linear in overvoltage, with smooth temperature
    dependence in light yield. Its second-order expansion supplies a search
    guide. Original-count model adequacy still decides whether a fit is usable.
    """
    counts = np.asarray(counts, float)
    temperature, bias = np.asarray(temperatures, float), np.asarray(biases, float)
    n = len(counts)
    empty = [None] * n
    if n < 16 or not np.isfinite(temperature).all() or not np.isfinite(bias).all():
        return empty
    if np.ptp(temperature) < 15 or np.ptp(bias) < .7:
        return empty
    anchors = []
    for index, y in enumerate(counts):
        try:
            candidates, _ = propose_photopeaks(x, y, config={
                "min_resolution": .03, "max_resolution": .6, **(settings or {}),
            })
        except PeakDetectionError:
            continue
        candidate = candidates[0]
        if (candidate.scales < 3 or candidate.shape_significance < 10
                or len(candidates) > 1 and candidates[1].score > .65 * candidate.score):
            continue
        anchors.append((index, candidate.center))
    if len(anchors) < 12:
        return empty
    t_scale, v_scale = float(np.std(temperature)), float(np.std(bias))
    t = (temperature - temperature.mean()) / t_scale
    v = (bias - bias.mean()) / v_scale
    design = np.column_stack([np.ones(n), v, t, v * t, t * t])
    guides = []
    for target in range(n):
        selected = [(index, center) for index, center in anchors if index != target]
        indices = np.array([item[0] for item in selected])
        values = np.sqrt([item[1] for item in selected])
        a = design[indices]
        if len(indices) < 12 or np.linalg.matrix_rank(a) < 5:
            guides.append(None)
            continue
        initial = np.linalg.lstsq(a, values, rcond=None)[0]
        fit = least_squares(lambda coefficients, a=a, values=values: a @ coefficients - values, initial,
                            loss="soft_l1", f_scale=.6, max_nfev=200)
        prediction = design @ fit.x
        residual = a @ fit.x - values
        inliers = np.abs(residual) < max(1.2, 3 * 1.4826 * np.median(np.abs(residual - np.median(residual))))
        spread = float(np.median(np.abs(residual[inliers] / values[inliers]))) if np.any(inliers) else 1.
        derivative_v = (fit.x[1] + fit.x[3] * t) / v_scale
        derivative_t = (fit.x[2] + fit.x[3] * v + 2 * fit.x[4] * t) / t_scale
        if (not fit.success or inliers.sum() < 12 or spread > .06
                or np.any(prediction <= 0) or np.any(derivative_v <= 0)
                or np.any(derivative_t >= 0)):
            guides.append(None)
            continue
        guides.append({"expected_center": float(prediction[target]**2),
                       "expected_tolerance": max(.12, min(.3, 8 * spread)),
                       "method": "leave_one_spectrum_out_sqrt_gain",
                       "target_excluded": True, "anchor_indices": indices[inliers].tolist(),
                       "median_relative_sqrt_residual": spread})
    return guides
