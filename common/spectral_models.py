"""Detector response strategies awaiting a dedicated calibration model."""


def detector_identity(payload, dataset=None, preset=None):
    """An editable output name does not change the detector response."""
    return ((preset or {}).get("payload") or (dataset or {}).get("profile")
            or (dataset or {}).get("source_payload") or payload)


def unsupported_spectral_model(payload: str, channel: int) -> str | None:
    """10B/11B plastic CH3 is outside the Gaussian photopeak workflow."""
    if str(payload).upper() in ("10B", "11B") and channel == 3:
        return "塑料闪烁体 · 康普顿响应，拟合方案待定"
    return None


def photopeak_channels(payload):
    return tuple(ch for ch in range(4) if not unsupported_spectral_model(payload, ch))


def pending_peak_record(payload, channel):
    """Empty slot for a fixed-channel legacy schema; no model was evaluated.

    Numeric zeros and the legacy fit_mode enum only encode an unavailable slot.
    Consumers must use placeholder/fit_performed/fit_quality before using it.
    """
    reason = unsupported_spectral_model(payload, channel)
    numeric = ("center", "center_err", "sigma", "sigma_err", "amplitude", "amplitude_err",
               "fwhm", "height", "resolution", "resolution_err", "rsquared",
               "temp", "temp_err", "bias", "bias_err")
    return {**dict.fromkeys(numeric, 0.), "channel": channel, "fit_mode": "gaus",
            "placeholder": True, "fit_performed": False,
            "unsupported_spectral_model": reason,
            "fit_quality": {"accepted": False, "status": "model_pending", "reason": reason,
                            "warnings": []}}
