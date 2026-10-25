"""Small L1 file/channel index, independent of surface fitting and event reads."""

from __future__ import annotations

import json
import math
from pathlib import Path

from ..common.fit_policy import quality_warnings
from ..common.spectral_models import detector_identity, unsupported_spectral_model
from ..common.utils import read_parquet_metadata


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _number(value):
    """Keep unavailable diagnostics missing, including legacy nonfinite values."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _condition(metadata, key, channel):
    value = metadata.get(key)
    if isinstance(value, list):
        value = value[channel] if channel < len(value) else None
    elif isinstance(value, dict):
        value = value.get(str(channel))
    return _number(value)


def overview(workbench, run_id):
    state = workbench.read(run_id)
    payload, root = state["payload"], workbench.root(run_id)
    response_payload = detector_identity(payload, state.get("dataset"), state.get("acquisition_preset"))
    adapter = workbench.adapter_for(state)
    l0 = adapter.tb_files(payload, root / "products")
    records = {}
    for directory in (
        adapter.historical_layer(payload, "TB_L1") / "json",
        root / "products" / payload / "tb" / "L1" / "json",
    ):
        for path in sorted(directory.glob("*_fit_results.l1.json")):
            rows = _read(path, [])
            if isinstance(rows, list):
                records[path.name.removesuffix("_fit_results.l1.json")] = rows
    attempts = {}
    for job in state.get("jobs", []):
        if job.get("layer") != "TB_L1":
            continue
        config = job.get("config", {})
        stems = config.get("only") or (
            list(l0) if job.get("scope") == "all" else
            state.get("inventory", {}).get("representative", {}).get("tb_stems", [])
        )
        result = job.get("result") or _read(root / "jobs" / job["id"] / "result.json", {})
        errors = result.get("errors", [])
        if not errors:
            errors = _read_error(job.get("error"))
        for stem in stems:
            for channel in config.get("fit_channels", range(4)):
                matching = [error for error in errors if isinstance(error, dict)
                            and _stem(error.get("stem") or error.get("file", "")) == stem
                            and error.get("channel") in (None, channel)]
                attempts[stem, channel] = {
                    "job_id": job["id"], "status": "failed" if matching else job["status"],
                    "error": matching[0].get("error") if matching else
                    job.get("error") if not errors and job["status"] == "failed" else None,
                }
    files = []
    pending = set(state.get("pending_windows", []))
    for stem in sorted(set(l0) | set(records)):
        channels = []
        metadata = None
        for channel in range(4):
            record = next((row for row in records.get(stem, []) if row.get("channel") == channel), {})
            point_id = f"{payload}:{stem}:{channel}"
            attempt = attempts.get((stem, channel), {})
            quality = record.get("fit_quality") or {}
            failure = state.get("tb_l1_failures", {}).get(point_id) or {}
            failed = (bool(failure) or attempt.get("status") == "failed"
                      or bool(attempt.get("error")) or quality.get("status") == "fit_failed")
            error = (failure.get("error") or attempt.get("error")
                     or (record.get("fit_failure") or {}).get("error")
                     or ("本次拟合失败" if failed else None))
            warnings = quality_warnings(record, min_rsquared=0.5) if not failed else []
            status = "failed" if failed else "suspect" if warnings else "fitted" if record.get("center") is not None else "not_fitted"
            conditions = {key: _number(record.get(key)) for key in ("temp", "temp_err", "bias", "bias_err")}
            if record.get("placeholder") and conditions["temp"] == conditions["bias"] == 0:
                # The legacy empty slot initializes all numeric fields to zero.
                # Only HK can establish whether a zero pair is a real condition.
                conditions = dict.fromkeys(conditions)
            # A condition without a fit still belongs at the measured HK point.
            # Read only schema metadata, once per file, and never infer from its name.
            if any(conditions[key] is None for key in ("temp", "bias")) and l0.get(stem):
                if metadata is None:
                    try:
                        metadata = read_parquet_metadata(l0[stem], "hk_data") or {}
                    except (OSError, ValueError, TypeError):
                        metadata = {}
                for key, value in conditions.items():
                    if value is None:
                        conditions[key] = _condition(metadata, key, channel)
            reason = unsupported_spectral_model(response_payload, channel)
            if reason:
                record, quality, status, attempt = {}, {}, "model_pending", {}
                error = None
                warnings = []
            accepted = quality.get("accepted")
            quality_accepted = False if status == "suspect" else accepted if isinstance(accepted, bool) and status not in ("model_pending", "failed") else None
            rejection_reasons = warnings  # Compatibility for older workbench clients.
            channels.append({
                **{key: record.get(key) for key in (
                    "center", "center_err", "sigma", "resolution", "rsquared",
                    "window_lo", "window_hi", "fit_mode", "fit_quality",
                )},
                **conditions,
                "id": point_id, "stem": stem, "channel": channel, "fit_status": status,
                "chi2_reduced": _number(quality.get("redchi")) if status not in ("model_pending", "failed") else None,
                "quality_accepted": quality_accepted, "rejection_reasons": rejection_reasons,
                "quality_warnings": warnings, "suspect": status == "suspect", "quality_policy": "warning_only",
                "pending_window": point_id in pending,
                "error": error, "attempt": attempt or None,
                "excluded": point_id in state.get("config", {}).get("excluded", []),
                "unsupported_spectral_model": reason,
            })
        supported = [item for item in channels if not item["unsupported_spectral_model"]]
        fitted = sum(item["fit_status"] in ("fitted", "suspect") and not item["pending_window"] for item in supported)
        attention = any(item["fit_status"] in ("failed", "suspect") for item in supported)
        needs_fit = any(item["pending_window"] for item in supported)
        files.append({"stem": stem, "has_l0": stem in l0, "channels": channels,
                      "fitted": fitted, "status": "attention" if attention else "fitted" if fitted == len(supported) and not needs_fit else "pending"})
    return {"files": files, "layer": "TB_L1", "payload": payload}


def _stem(value):
    return Path(value).name.removesuffix(".parquet").removesuffix(".l0")


def _read_error(value):
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []
