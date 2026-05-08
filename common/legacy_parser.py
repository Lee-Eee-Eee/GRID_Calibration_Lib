"""对接原始 parse_grid_data 代码的适配层。"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd


def _find_parser_resource_dir(root_dir: Path) -> Path:
    internal = root_dir / "calibration_lib" / "resources" / "homework2_legacy"
    if (internal / "hnu_packet.xml").exists() and (internal / "parse_grid_data.py").exists():
        return internal

    base = root_dir / "1415B_json" / "homework2"
    if (base / "hnu_packet.xml").exists() and (base / "parse_grid_data.py").exists():
        return base

    # 向后兼容：扫描旧目录结构
    scan_base = root_dir / "1415B_json"
    candidates = list(scan_base.rglob("hnu_packet.xml"))
    if not candidates:
        raise FileNotFoundError(
            "hnu_packet.xml not found in calibration_lib/resources/homework2_legacy or 1415B_json/homework2"
        )
    return candidates[0].parent


def _ensure_parser_path(root_dir: Path) -> Path:
    parser_dir = _find_parser_resource_dir(root_dir)
    if str(parser_dir) not in sys.path:
        sys.path.insert(0, str(parser_dir))
    return parser_dir


def _parse_silent(parse_fn, path: Path, data_tag: str, xml_file: Path):
    with contextlib.redirect_stdout(io.StringIO()):
        return parse_fn(
            str(path),
            data_tag=data_tag,
            endian="MSB",
            xml_file=str(xml_file),
        )[0]


def _extract_time_axis(event_data: Dict[str, Any], size: int) -> np.ndarray:
    for key in ("utc", "utc_time", "timestamp"):
        arr = np.asarray(event_data.get(key, []), dtype=float)
        if arr.size == size:
            return arr
    return np.arange(size, dtype=float)


def _tail_mean(arr: np.ndarray, n: int = 60) -> float:
    if arr.size == 0:
        return float("nan")
    tail = arr[-min(n, arr.size) :]
    return float(np.mean(tail))


def _tail_stats(arr: np.ndarray, n: int = 60) -> Dict[str, float]:
    if arr.size == 0:
        return {"mean": float("nan"), "std": float("nan"), "n": 0}
    tail = arr[-min(n, arr.size) :]
    return {
        "mean": float(np.mean(tail)),
        "std": float(np.std(tail, ddof=1)) if tail.size > 1 else 0.0,
        "n": int(tail.size),
    }


def parse_event_hk_to_dataframe(
    event_path: Path,
    hk_path: Path,
    root_dir: Path,
    event_tag: str,
    hk_tag: str,
    include_per_point_tv: bool,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """解析单对 event/hk，返回统一 DataFrame 与 metadata。"""
    parser_dir = _ensure_parser_path(root_dir)

    from parse_grid_data import parse_grid_data_new  # type: ignore

    xml_file = parser_dir / "hnu_packet.xml"

    event_data = _parse_silent(parse_grid_data_new, event_path, event_tag, xml_file)
    hk_data = _parse_silent(parse_grid_data_new, hk_path, hk_tag, xml_file)

    ch = np.asarray(event_data.get("channel_n", []), dtype=int)
    data_max = np.asarray(event_data.get("data_max", []), dtype=float)
    data_base = np.asarray(event_data.get("data_base", []), dtype=float)
    amp = data_max - data_base / 4.0

    n_evt = min(ch.size, amp.size)
    ch = ch[:n_evt]
    amp = amp[:n_evt]
    utc = _extract_time_axis(event_data, n_evt)

    temp_by_ch: Dict[int, float] = {}
    bias_by_ch: Dict[int, float] = {}
    channel_meta: Dict[str, Dict[str, float]] = {}

    temp = np.full(n_evt, np.nan, dtype=float)
    bias = np.full(n_evt, np.nan, dtype=float)

    for c in range(4):
        t_raw = np.asarray(hk_data.get(f"sipm_temp{c}", []), dtype=float)
        v_raw = np.asarray(hk_data.get(f"sipm_voltage{c}", []), dtype=float)
        t_c = t_raw / 100.0 - 273.15
        v_c = v_raw / 1000.0
        t_stat = _tail_stats(t_c, n=60)
        v_stat = _tail_stats(v_c, n=60)

        temp_mean = float(t_stat["mean"])
        bias_mean = float(v_stat["mean"])
        temp_by_ch[c] = temp_mean
        bias_by_ch[c] = bias_mean
        channel_meta[str(c)] = {
            "temp": temp_mean,
            "temp_err": float(t_stat["std"]),
            "temp_n": int(t_stat["n"]),
            "bias": bias_mean,
            "bias_err": float(v_stat["std"]),
            "bias_n": int(v_stat["n"]),
            "temp_n_raw": int(t_raw.size),
            "bias_n_raw": int(v_raw.size),
        }

        ch_idx = np.where(ch == c)[0]
        if ch_idx.size == 0:
            continue

        if include_per_point_tv:
            if t_c.size == 0:
                temp[ch_idx] = np.nan
            elif t_c.size == 1:
                temp[ch_idx] = float(t_c[0])
            else:
                src = np.linspace(0.0, 1.0, t_c.size)
                dst = np.linspace(0.0, 1.0, ch_idx.size)
                temp[ch_idx] = np.interp(dst, src, t_c)

            if v_c.size == 0:
                bias[ch_idx] = np.nan
            elif v_c.size == 1:
                bias[ch_idx] = float(v_c[0])
            else:
                src = np.linspace(0.0, 1.0, v_c.size)
                dst = np.linspace(0.0, 1.0, ch_idx.size)
                bias[ch_idx] = np.interp(dst, src, v_c)
        else:
            temp[ch_idx] = temp_mean
            bias[ch_idx] = bias_mean

    if include_per_point_tv:
        valid = np.isfinite(amp) & np.isfinite(temp) & np.isfinite(bias)
        df = pd.DataFrame(
            {
                "amp": amp[valid],
                "ch": ch[valid].astype(int),
                "utc": utc[valid],
                "temp": temp[valid],
                "bias": bias[valid],
            }
        )
    else:
        valid = np.isfinite(amp)
        df = pd.DataFrame(
            {
                "amp": amp[valid],
                "ch": ch[valid].astype(int),
                "utc": utc[valid],
            }
        )

    metadata = {
        "event_file": event_path.name,
        "hk_file": hk_path.name,
        "event_tag": event_tag,
        "hk_tag": hk_tag,
        "n_events_raw": int(n_evt),
        "n_events_valid": int(len(df)),
        "include_per_point_tv": bool(include_per_point_tv),
        "channels": channel_meta,
    }
    return df, metadata
