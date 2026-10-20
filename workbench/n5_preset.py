"""Build a portable N5 manifest from the copied official acquisition archive."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .acquisition import default_profile, discover

N5_EC_REASON = "N5 X 射线需要另一照射文件中同物理通道的背景及曝光归一化，当前配对接口待接入"


def _one_pair(directory: Path, base: str) -> tuple[Path, Path]:
    if not base or Path(base).name != base or any(char in base for char in "*?[]"):
        raise ValueError("N5 manifest base 不能为空或包含路径/通配符")
    matches = []
    for pattern in (f"{base}_sci_*.event.dat", f"{base}_hk_*.hk"):
        candidates = sorted(path for path in directory.glob(pattern) if path.is_file() and path.stat().st_size)
        if len(candidates) != 1:
            raise ValueError(f"N5 {base} 需要唯一已复制 sci/hk 配对，{pattern} 匹配 {len(candidates)} 个")
        matches.append(candidates[0])
    return tuple(matches)


def build_n5_profile(raw_root: Path) -> tuple[dict, dict]:
    """Index exactly the 48 official TB conditions and unambiguous X-ray pairs.

    Paths inside the profile are relative to the archive root. No source file is
    modified, and no source exposure/background association is guessed.
    """
    root = Path(raw_root).resolve()
    directory = root / "温度偏压/N5"
    if not directory.is_dir():
        raise ValueError(f"N5 官方 TB 目录不存在: {directory}")
    observations, coordinates = [], set()
    for manifest in sorted(directory.glob("*_manifest.json")):
        metadata = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if metadata.get("official") is not True:
            continue
        if metadata.get("payload") != "N5":
            raise ValueError(f"官方 manifest 载荷编号与 N5 不符: {manifest.name}")
        coordinate = (float(metadata["temp"]), float(metadata["bias"]))
        if coordinate in coordinates:
            raise ValueError(f"N5 官方工况重复，需明确选择采集版本: {coordinate}")
        coordinates.add(coordinate)
        base = metadata.get("base", "")
        event, hk = _one_pair(directory, base)
        observations.append({"kind": "tb", "stem": base,
                             "event": event.relative_to(root).as_posix(), "hk": hk.relative_to(root).as_posix(),
                             "temp": coordinate[0], "bias": coordinate[1], "source": "Am241",
                             "energy_keV": 59.54, "peak_energies_keV": [59.54],
                             "manifest_file": manifest.relative_to(root).as_posix(),
                             "acquire_span_s": metadata.get("acquire_span_s"), "official": True})
    expected = {(temp, bias) for temp in (-20, -10, 0, 10, 20, 30)
                for bias in (26.5, 27, 27.5, 28, 28.3, 28.5, 28.7, 29)}
    if coordinates != expected:
        raise ValueError(f"N5 官方 TB 应为 48 个工况；缺失 {sorted(expected - coordinates)}，额外 {sorted(coordinates - expected)}")
    observations.sort(key=lambda row: (row["temp"], row["bias"]))
    xray, warnings = {}, []
    x_directory = root / "x/N5"
    for event in sorted(x_directory.glob("*_sci_*.event.dat")):
        match = re.match(r"^X(?P<energy>\d+(?:\.\d+)?)keV_CH(?P<channel>[0-3])_N5_", event.name, re.I)
        if not match:
            continue
        base = event.name.split("_sci_", 1)[0]
        pair_event, hk = _one_pair(x_directory, base)
        key = (float(match["energy"]), int(match["channel"]))
        if key in xray:
            raise ValueError(f"N5 X 射线能量/通道存在多次采集，请选择版本后再初始化: {key}")
        xray[key] = {"kind": "xray", "event": pair_event.relative_to(root).as_posix(),
                     "hk": hk.relative_to(root).as_posix(), "energy_keV": key[0], "channel": key[1]}
    if not xray:
        warnings.append("未索引 N5 X 射线文件；TB 预设可使用")
    observations.extend(xray[key] for key in sorted(xray))
    profile = default_profile("14B")
    protocol = Path(__file__).resolve().parents[1] / "resources/legacy/hnu_hk182_packet.xml"
    profile["xml"] = {role: {"source_path": str(protocol), "text": None} for role in ("event", "hk")}
    profile["discovery"].update(mode="manifest", manifest=observations)
    profile["discovery"]["sources"] = [{"name": "Am241", "aliases": ["Am-241", "am241"], "energies_keV": [59.54]}]
    profile["fitting"] = {"tb_histogram": {"min": 0, "max": 65536, "bin_width": 1}}
    profile["reading_basis"] = {
        "payload": "N5", "conditions": "HK physical telemetry; manifest temp/bias are nominal acquisition settings",
        "temperature": "sipm_temp/100-273.15 degC, compared with report_N4N5 measured SiPM temperature",
        "bias": "HK voltage monitor /1000 V; no undocumented current-drop correction",
        "manifest_selection": "official=true, unique six temperatures by eight bias settings",
        "line_energy_keV": 59.54,
    }
    profile["processing_capabilities"] = {
        "EC_L2": {"supported": False, "reason": N5_EC_REASON},
        "EC_L3": {"supported": False, "reason": "依赖有效 N5 EC L2"},
    }
    preview = discover(root, profile)
    if preview["errors"]:
        raise ValueError(f"N5 读取配置检查失败: {preview['errors']}")
    return profile, {"raw_root": str(root), "counts": preview["counts"], "warnings": warnings,
                     "tb_source": "Am241", "tb_line_energy_keV": 59.54,
                     "voltage_convention": "HK monitored voltage", "EC_L2_reason": N5_EC_REASON}
