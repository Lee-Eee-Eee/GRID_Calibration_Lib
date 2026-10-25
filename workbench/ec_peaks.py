"""Editable EC source lines and ADC windows, independent of raw decoding."""

from __future__ import annotations

import math
import re

from ..common.spectral_models import photopeak_channels
from ..common.utils import read_json, read_parquet_metadata
from .acquisition import default_profile

SOURCE_FILE = re.compile(r"^src_(?P<name>.+)_(?P<minutes>\d+(?:\.\d+)?)m_corr\.l1\.parquet$")


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} 必须为正的有限数值")
    return float(value)


def _key(value):
    if (not isinstance(value, str) or not value.startswith("src_") or value.startswith("src_bkg_")
            or value != value.strip() or len(value) > 160 or any(char in value for char in "/\\:\x00")):
        raise ValueError("源谱标识必须为 src_<source>，不能包含路径")
    return value


def _same_energy(left, right):
    return math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-8)


def normalize_peak_sets(value):
    """Normalize a complete per-run source-line override without changing L0."""
    if not isinstance(value, dict) or len(value) > 256:
        raise ValueError("ec_peak_sets 必须为源谱配置对象")
    result = {}
    for key, entry in value.items():
        _key(key)
        if not isinstance(entry, dict) or set(entry) - {"source_name", "lines"}:
            raise ValueError("源谱配置仅支持 source_name 和 lines")
        source_name = entry.get("source_name", key.removeprefix("src_"))
        if not isinstance(source_name, str) or not source_name.strip() or len(source_name) > 120:
            raise ValueError("发射源名称不能为空")
        lines = entry.get("lines")
        if not isinstance(lines, list) or len(lines) > 64:
            raise ValueError("lines 必须为最多64条能线的列表")
        normalized = []
        for line in lines:
            if not isinstance(line, dict) or set(line) - {"energy_keV", "windows", "centers"}:
                raise ValueError("能线仅支持 energy_keV、windows、centers")
            energy = _number(line.get("energy_keV"), "能线 keV")
            if any(_same_energy(energy, old["energy_keV"]) for old in normalized):
                raise ValueError("同一源谱不能配置重复能量")
            windows, centers = line.get("windows", {}), line.get("centers", {})
            if not isinstance(windows, dict) or not isinstance(centers, dict):
                raise ValueError("windows 和 centers 必须按通道保存")
            row = {"energy_keV": energy, "windows": {}, "centers": {}}
            for channel, window in windows.items():
                if str(channel) not in ("0", "1", "2", "3") or not isinstance(window, dict):
                    raise ValueError("窗口通道必须为0–3")
                if set(window) != {"lo", "hi"}:
                    raise ValueError("每个窗口必须提供 lo/hi")
                lo, hi = window["lo"], window["hi"]
                if (isinstance(lo, bool) or not isinstance(lo, (int, float)) or not math.isfinite(lo)
                        or lo < 0 or _number(hi, "窗口上限") <= lo):
                    raise ValueError("窗口必须满足 0 ≤ lo < hi")
                row["windows"][str(channel)] = {"lo": float(lo), "hi": float(hi)}
            for channel, center in centers.items():
                if str(channel) not in ("0", "1", "2", "3"):
                    raise ValueError("峰位通道必须为0–3")
                row["centers"][str(channel)] = _number(center, "期望 ADC 峰位")
            normalized.append(row)
        result[key] = {"source_name": source_name.strip(), "lines": sorted(normalized, key=lambda row: row["energy_keV"])}
    return result


def _source_alias(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _definitions(profile):
    # Defaults supply a named-source library; a payload preset can override it.
    sources = default_profile().get("discovery", {}).get("sources", [])
    sources = [*sources, *((profile or {}).get("discovery", {}).get("sources") or [])]
    aliases = {}
    for source in sources:
        for alias in [source["name"], *(source.get("aliases") or [])]:
            aliases[_source_alias(alias)] = source
    return aliases


def source_definition(name, profile=None):
    """Resolve a source spelling through the existing preset source library."""
    return _definitions(profile).get(_source_alias(name))


def _layers(adapter, payload, products, layer):
    current = adapter._layer_dir(products, payload, layer)
    history = adapter.historical_layer(payload, layer)
    return list(dict.fromkeys([current, history]))


def _files(adapter, payload, products, layer, kind, pattern):
    paths = {}
    for directory in reversed(_layers(adapter, payload, products, layer)):
        paths.update({path.name: path for path in (directory / kind).glob(pattern)})
    return [paths[name] for name in sorted(paths)]


def _energy_lines(values):
    result = []
    for value in values or []:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            if not any(_same_energy(value, energy) for energy in result):
                result.append(float(value))
    return result


def catalog(adapter, payload, product_root, *, profile=None, config=None):
    """Discover source spectra from names/schema metadata, without event reads."""
    config = config or {}
    peak_sets = normalize_peak_sets(config.get("ec_peak_sets", {}))
    profile = profile if profile is not None else getattr(adapter, "acquisition", None)
    definitions = _definitions(profile)
    preset_definitions = {_source_alias(alias): source
                          for source in (profile or {}).get("discovery", {}).get("sources", [])
                          for alias in [source["name"], *(source.get("aliases") or [])]}
    spectra = {}
    identity = adapter.detector_payload(payload) if hasattr(adapter, "detector_payload") else payload
    channels = list(photopeak_channels(identity))
    for path in _files(adapter, payload, product_root, "EC_L1", "parquet", "src_*_corr.l1.parquet"):
        match = SOURCE_FILE.fullmatch(path.name)
        if not match or match["name"].lower() == "bkg":
            continue
        key, name = f"src_{match['name']}", match["name"]
        try:
            metadata = read_parquet_metadata(path, "tb_correction") or {}
        except (OSError, ValueError, TypeError):
            metadata = {}
        definition = definitions.get(_source_alias(name))
        explicit = preset_definitions.get(_source_alias(name))
        energies = (_energy_lines(explicit.get("energies_keV")) if explicit else
                    _energy_lines(metadata.get("peak_energies_keV")))
        if not energies and definition:
            energies = _energy_lines(definition.get("energies_keV"))
        origin = "preset" if explicit else "metadata" if metadata.get("peak_energies_keV") else "filename" if definition else "manual"
        entry = spectra.setdefault(key, {"energy_key": key, "source_name": (definition or {}).get("name", name),
                                        "signal_file": path.name, "source_path": str(path), "source_files": [],
                                        "available_channels": channels, "origin": origin, "lines": [], "_records": []})
        entry["source_files"].append({"name": path.name, "path": str(path),
                                      "exposure_minutes": metadata.get("exposure_minutes", float(match["minutes"]))})
        for energy in energies:
            if not any(_same_energy(row["energy_keV"], energy) for row in entry["lines"]):
                entry["lines"].append({"energy_keV": energy, "windows": {}, "centers": {}, "origin": origin})
    for path in _files(adapter, payload, product_root, "EC_L2", "json", "src_*_fit_results.l2.json"):
        key = path.name.removesuffix("_fit_results.l2.json")
        if key.startswith("src_bkg_"):
            continue
        try:
            records = read_json(path)
        except (OSError, ValueError):
            continue
        if not isinstance(records, list):
            continue
        entry = spectra.setdefault(key, {"energy_key": key, "source_name": key.removeprefix("src_"),
                                        "signal_file": None, "source_path": None, "source_files": [],
                                        "available_channels": channels, "origin": "result", "lines": [], "_records": []})
        entry["_records"] = records
        for record in records:
            energies = _energy_lines([record.get("E")])
            if not energies:
                continue
            energy = energies[0]
            line = next((row for row in entry["lines"] if _same_energy(row["energy_keV"], energy)), None)
            if line is None:
                line = {"energy_keV": energy, "windows": {}, "centers": {}, "origin": "result"}
                entry["lines"].append(line)
            channel = str(record.get("channel"))
            if channel not in ("0", "1", "2", "3"):
                continue
            lo, hi = record.get("lo"), record.get("hi")
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi:
                line["windows"][channel] = {"lo": float(lo), "hi": float(hi)}
    for key, saved in peak_sets.items():
        entry = spectra.setdefault(key, {"energy_key": key, "signal_file": None, "source_path": None,
                                        "source_files": [], "available_channels": channels, "_records": []})
        entry.update(source_name=saved["source_name"], origin="run", lines=[{**row, "origin": "run"} for row in saved["lines"]])
    for key, entry in spectra.items():
        for line in entry["lines"]:
            for channel in range(4):
                point = f"{payload}:ec:{key}:{channel}:{line['energy_keV']:g}"
                if point in config.get("ec_windows", {}):
                    line["windows"][str(channel)] = dict(config["ec_windows"][point])
        entry["lines"].sort(key=lambda row: row["energy_keV"])
        entry.pop("_records", None)
    return {"spectra": [spectra[key] for key in sorted(spectra)]}


def draft_points(source_catalog, payload, channel, existing_points=()):
    """Add selectable unperformed lines while retaining exact fitted records."""
    if type(channel) is not int or channel not in range(4):
        raise ValueError("Channel must be 0, 1, 2 or 3")
    existing_points = [point for point in existing_points if point.get("channel") == channel]
    existing = {point["id"]: point for point in existing_points}
    by_source = {source["energy_key"]: source for source in source_catalog["spectra"]}
    rows = [point for point in existing_points if point.get("energy_key", point.get("stem")) not in by_source]
    for key, source in by_source.items():
        peers = [point for point in existing_points if point.get("energy_key", point.get("stem")) == key]
        width = next((point["bin_width"] for point in peers if point.get("bin_width")), 4.)
        for line in source["lines"]:
            energy = line["energy_keV"]
            identity = f"{payload}:ec:{key}:{channel}:{energy:g}"
            window, center = line["windows"].get(str(channel)), line["centers"].get(str(channel))
            point = existing.get(identity)
            if point is not None:
                rows.append({**point, "configured": True, "draft_window": window, "draft_center": center})
                continue
            rows.append({"id": identity, "stem": key, "energy_key": key, "channel": channel, "E": energy,
                         "source": "src", "source_name": source["source_name"], "signal_file": source["signal_file"],
                         "source_path": source["source_path"], "l1_stem": (source["signal_file"] or "").removesuffix("_corr.l1.parquet"),
                         "bin_width": width, "lo": window["lo"] if window else None, "hi": window["hi"] if window else None,
                         "center": None, "sigma": None, "center_err": None, "fit_status": "not_fitted",
                         "fit_performed": False, "quality_pass": False, "quality_suspect": False, "quality_reasons": [],
                         "draft": True, "configured": True, "draft_window": window, "draft_center": center})
    return sorted(rows, key=lambda row: (row["E"], row["channel"], row["stem"]))
