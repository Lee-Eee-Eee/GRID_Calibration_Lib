"""Small L1 file/channel index, independent of surface fitting and event reads."""

from __future__ import annotations

import json
from pathlib import Path


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def overview(workbench, run_id):
    state = workbench.read(run_id)
    payload, root = state["payload"], workbench.root(run_id)
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
        for channel in range(4):
            record = next((row for row in records.get(stem, []) if row.get("channel") == channel), {})
            point_id = f"{payload}:{stem}:{channel}"
            attempt = attempts.get((stem, channel), {})
            quality = record.get("fit_quality", {})
            status = "failed" if attempt.get("error") else "rejected" if quality.get("accepted") is False else "fitted" if record.get("center") is not None else "not_fitted"
            channels.append({
                **{key: record.get(key) for key in (
                    "center", "center_err", "sigma", "resolution", "rsquared", "temp", "bias",
                    "window_lo", "window_hi", "fit_mode", "fit_quality",
                )},
                "id": point_id, "stem": stem, "channel": channel, "fit_status": status,
                "pending_window": point_id in pending,
                "error": attempt.get("error"), "attempt": attempt or None,
                "excluded": point_id in state.get("config", {}).get("excluded", []),
            })
        fitted = sum(item["fit_status"] == "fitted" and not item["pending_window"] for item in channels)
        attention = any(item["fit_status"] in ("failed", "rejected") for item in channels)
        needs_fit = any(item["pending_window"] for item in channels)
        files.append({"stem": stem, "has_l0": stem in l0, "channels": channels,
                      "fitted": fitted, "status": "attention" if attention else "fitted" if fitted == 4 and not needs_fit else "pending"})
    return {"files": files, "layer": "TB_L1", "payload": payload}


def _stem(value):
    return Path(value).name.removesuffix(".parquet").removesuffix(".l0")


def _read_error(value):
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []
