"""Read and analyze migrated reference histograms using the shared peak detector."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..common.peak_finding import PeakDetectionError, detect_photopeak
from ..common.reference_spectra import read_reference_spectrum, scan_reference_directory


class ReferenceLibrary:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.default = self.workspace / "data" / "imported" / "GRID-11B_HPGe_data"

    def _path(self, value, default=None):
        path = Path(value).expanduser() if value else default
        if path is None:
            raise ValueError("请选择参考谱文件")
        return (self.workspace / path).resolve() if not path.is_absolute() else path.resolve()

    def catalog(self, directory=None):
        path = self._path(directory, self.default)
        return {"directory": str(path), "files": scan_reference_directory(path)}

    def spectrum(self, path):
        return read_reference_spectrum(self._path(path))

    def analyze(self, path):
        source = self.spectrum(path)
        settings = {"infer_noise": False, "max_resolution": 1.2}
        try:
            _, detection = detect_photopeak(
                np.asarray(source["x"], dtype=float), np.asarray(source["y"], dtype=float), config=settings
            )
            status, reason = "resolved", None
        except PeakDetectionError as exc:
            status, reason = "needs_review", str(exc)
            detection = getattr(exc, "diagnostics", {"candidates": [], "warnings": [reason]})
        identifier = datetime.now().strftime("%Y%m%d-%H%M%S") + "-reference-" + uuid.uuid4().hex[:10]
        directory = self.workspace / "runs" / identifier
        directory.mkdir(parents=True)
        record = directory / "analysis.json"
        result = {
            "id": identifier,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "reason": reason,
            "source": {key: value for key, value in source.items() if key not in ("x", "y")},
            "config": settings,
            "peak_detection": detection,
            "record_path": str(record),
        }
        record.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        return result
