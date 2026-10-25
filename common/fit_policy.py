"""Quality diagnostics are advisory; unavailable fits cannot supply parameters."""

import math


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def unavailable_fit(record):
    quality = record.get("fit_quality") or {}
    return bool(record.get("placeholder") or record.get("fit_performed") is False
                or record.get("unsupported_spectral_model")
                or record.get("fit_status") in ("failed", "not_fitted", "model_pending")
                or quality.get("status") in ("fit_failed", "not_fitted", "model_pending"))


def usable_peak(record):
    """A warning never invalidates an otherwise finite positive peak center."""
    return not unavailable_fit(record) and finite_number(record.get("center")) and record["center"] > 0


def quality_warnings(record, *, min_rsquared=None):
    quality = record.get("fit_quality") or {}
    warnings = [item for item in (quality.get("warnings") or []) if isinstance(item, str)]
    reason = quality.get("reason")
    if isinstance(reason, str) and reason not in warnings and not unavailable_fit(record):
        warnings.append(reason)
    if (quality.get("accepted") is False or quality.get("suspect")
            or quality.get("status") in ("rejected", "suspect")) and not warnings and not unavailable_fit(record):
        warnings.append("Saved fit diagnostics flag a possible problem")
    r2 = record.get("rsquared")
    if min_rsquared is not None and finite_number(r2) and r2 < min_rsquared:
        warnings.append(f"R² < {min_rsquared:g}")
    return list(dict.fromkeys(warnings))
