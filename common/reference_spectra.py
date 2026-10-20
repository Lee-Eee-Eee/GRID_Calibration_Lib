"""Read HPGe TKA reference spectra without treating them as GRID event packets.

The acquisition's legacy ``reader05.HPGeDataReadout`` uses the first value as
live time and clears values 0 and 1, retaining the original channel numbering.
The second header's meaning is retained as unknown rather than inferred.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

NAME_METADATA = re.compile(
    r"^(?P<energy>\d+(?:\.\d+)?)keV(?:-(?P<tube_kv>\d+(?:\.\d+)?)kV)?"
    r"(?:-(?P<tube_ma>\d+(?:\.\d+)?)mA)?(?:-(?P<seconds>\d+(?:\.\d+)?)s)?$", re.IGNORECASE
)
MAX_TKA_BYTES = 16 * 1024 * 1024


def read_reference_spectrum(path: Path | str) -> dict:
    """Return JSON-ready channel/count arrays and explicit reference metadata."""
    source = Path(path).resolve(strict=True)
    if not source.is_file() or source.suffix.lower() != ".tka":
        raise ValueError("Only HPGe ASCII TKA reference spectra are supported")
    if source.stat().st_size > MAX_TKA_BYTES:
        raise ValueError("TKA reference exceeds the 16 MiB size limit")
    raw = source.read_bytes()
    try:
        lines = raw.decode("utf-8-sig").splitlines()
        values = [float(value.strip()) for value in lines]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("TKA requires one numeric value per line") from exc
    if len(values) < 3 or not all(math.isfinite(value) for value in values):
        raise ValueError("TKA requires two finite headers followed by counts")
    if values[0] <= 0:
        raise ValueError("TKA live time must be positive")
    if any(value < 0 or not value.is_integer() for value in values[2:]):
        raise ValueError("TKA counts must be nonnegative integers")
    counts = [0, 0, *(int(value) for value in values[2:])]
    metadata = {
        "kind": "hpge_reference", "detector": "HPGe", "x_quantity": "channel", "x_unit": "ADC channel",
        "y_quantity": "counts", "y_unit": "counts", "channel_count": len(counts),
        "live_time_seconds": values[0], "second_header_value": values[1],
        "second_header_meaning": "unknown", "header_channels_cleared": [0, 1],
        "total_counts": sum(counts), "max_count_channel": counts.index(max(counts)),
        "energy_axis_calibrated": False,
        "format_basis": "legacy reader05.HPGeDataReadout: live time at index 0; clear indices 0 and 1",
    }
    match = NAME_METADATA.fullmatch(source.stem)
    if match:
        for key, field in {
            "energy": "nominal_energy_keV", "tube_kv": "tube_voltage_kV",
            "tube_ma": "tube_current_mA", "seconds": "filename_exposure_seconds",
        }.items():
            if match[key] is not None:
                metadata[field] = float(match[key])
        metadata["nominal_energy_source"] = "filename"
    cnf = next((p for p in source.parent.iterdir() if p.is_file() and p.stem == source.stem and p.suffix.lower() == ".cnf"), None)
    if cnf:
        metadata["binary_companion"] = str(cnf)
    return {
        "x": list(range(len(counts))), "y": counts, "metadata": metadata,
        "filename": source.name, "path": str(source), "sha256": hashlib.sha256(raw).hexdigest(),
    }


def scan_reference_directory(path: Path | str) -> list[dict]:
    """Catalog TKA files deterministically; retain malformed files as errors."""
    root = Path(path).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Reference directory must be a directory")
    rows = []
    for source in sorted(root.rglob("*")):
        if not source.is_file() or source.suffix.lower() != ".tka":
            continue
        if source.is_symlink() or root not in source.resolve().parents:
            raise ValueError(f"Reference file escapes selected directory: {source}")
        try:
            spectrum = read_reference_spectrum(source)
            row = {key: value for key, value in spectrum.items() if key not in {"x", "y"}}
            row["status"] = "ready"
        except (OSError, ValueError) as exc:
            row = {"filename": source.name, "path": str(source), "metadata": {}, "status": "invalid", "error": str(exc)}
        row["relative_path"] = source.relative_to(root).as_posix()
        rows.append(row)
    return sorted(rows, key=lambda row: (row["metadata"].get("nominal_energy_keV", float("inf")), row["relative_path"]))
