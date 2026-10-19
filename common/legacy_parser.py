"""Adapter that wraps the legacy GRID packet parser bundled under
``calibration_lib/resources/legacy``."""

from __future__ import annotations

import contextlib
import hashlib
import io
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def _find_parser_resource_dir(root_dir: Path) -> Path:
    """Locate the bundled GRID-packet parser resources.

    Order of preference:
      1) ``calibration_lib/resources/legacy`` shipped with this library.
      2) ``1415B_json/homework2`` from the original GRID monorepo (kept as a
         fallback for users who run this code inside that monorepo).
    """
    # Resolve against this installed package before considering workspace fallbacks.
    for internal in (
        Path(__file__).resolve().parents[1] / "resources" / "legacy",
        root_dir / "calibration_lib" / "resources" / "legacy",
    ):
        if (internal / "hnu_packet.xml").exists() and (internal / "parse_grid_data.py").exists():
            return internal

    monorepo_fallback = root_dir / "1415B_json" / "homework2"
    if (monorepo_fallback / "hnu_packet.xml").exists() and (
        monorepo_fallback / "parse_grid_data.py"
    ).exists():
        return monorepo_fallback

    raise FileNotFoundError(
        "GRID packet parser not found. Expected at "
        "calibration_lib/resources/legacy/{hnu_packet.xml,parse_grid_data.py}."
    )


def _ensure_parser_path(root_dir: Path) -> Path:
    parser_dir = _find_parser_resource_dir(root_dir)
    if str(parser_dir) not in sys.path:
        sys.path.insert(0, str(parser_dir))
    return parser_dir


def _parse_silent(
    parse_fn, path: Path, data_tag: str, xml_file: Path,
    endian: str = "MSB", multi_evt: int | None = None, multi_step: int | None = None,
):
    options: dict[str, Any] = {}
    if multi_evt is not None:
        options["multi_evt"] = multi_evt
    if multi_step is not None:
        options["multi_step"] = multi_step
    with contextlib.redirect_stdout(io.StringIO()):
        return parse_fn(
            str(path),
            data_tag=data_tag,
            endian=endian,
            xml_file=str(xml_file),
            **options,
        )[0]


def _extract_time_axis(
    event_data: dict[str, Any], size: int, keys: list[str] | None = None,
) -> np.ndarray:
    for key in keys or ("utc", "utc_time", "timestamp"):
        arr = np.asarray(event_data.get(key, []), dtype=float).reshape(-1)
        if arr.size == size:
            return arr
    return np.arange(size, dtype=float)


def _tail_mean(arr: np.ndarray, n: int = 60) -> float:
    if arr.size == 0:
        return float("nan")
    tail = arr[-min(n, arr.size) :]
    return float(np.mean(tail))


def _tail_stats(arr: np.ndarray, n: int = 60) -> dict[str, float]:
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
    input_profile: dict[str, Any] | None = None,
    xml_paths: dict[str, Path] | None = None,
    observation: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """解析单对 event/hk，返回统一 DataFrame 与 metadata。"""
    parser_dir = _ensure_parser_path(root_dir)

    from parse_grid_data import parse_grid_data_new  # type: ignore

    profile = input_profile or {}
    settings = profile.get("parser", {})
    fields = settings.get("fields", {})
    transform = settings.get("transform", {})
    conditions = settings.get("conditions", {})
    observation = observation or {}
    event_tag = settings.get("event_tag", event_tag)
    hk_tag = settings.get("hk_tag", hk_tag)
    endian = settings.get("endian", "MSB")
    xml_files: dict[str, Path] = {}
    for kind in ("event", "hk"):
        spec = profile.get("xml", {}).get(kind, {})
        selected = (xml_paths or {}).get(kind) or spec.get("snapshot_path") or spec.get("source_path")
        xml_files[kind] = Path(selected) if selected else parser_dir / "hnu_packet.xml"
        if not xml_files[kind].is_file():
            raise FileNotFoundError(f"{kind} XML 文件不存在: {xml_files[kind]}")

    def parse(path: Path, kind: str, tag: str):
        options = {
            name: settings[f"{kind}_{name}"]
            for name in ("multi_evt", "multi_step")
            if settings.get(f"{kind}_{name}") is not None
        }
        if endian != "MSB":
            options["endian"] = endian
        return _parse_silent(parse_grid_data_new, path, tag, xml_files[kind], **options)

    event_data = parse(event_path, "event", event_tag)
    hk_data = parse(hk_path, "hk", hk_tag)

    required = {key: fields.get(key, default) for key, default in (
        ("channel", "channel_n"), ("max", "data_max"), ("base", "data_base"),
    )}
    for logical, name in required.items():
        if name not in event_data:
            raise ValueError(f"事件包缺少 {logical} 字段 {name!r}；请核对 XML、包标签和字段映射")
    ch = np.asarray(event_data[required["channel"]], dtype=int).reshape(-1)
    data_max = np.asarray(event_data[required["max"]], dtype=float).reshape(-1)
    data_base = np.asarray(event_data[required["base"]], dtype=float).reshape(-1)
    if not (ch.size == data_max.size == data_base.size):
        raise ValueError("事件通道、峰值和基线字段长度不一致；请核对 XML 和重复事件参数")
    if not ch.size:
        raise ValueError("事件文件没有解析出事件；请核对 XML、包标签和字节序")
    if np.any((ch < 0) | (ch > 3)):
        raise ValueError("事件通道超出 0–3；请核对 XML、字段映射和字节序")
    base_divisor = float(transform.get("base_divisor", 4.0))
    ccm_divisor = float(transform.get("ccm_divisor", 65535.0))
    if base_divisor == 0 or ccm_divisor == 0:
        raise ValueError("基线和 CCM 除数必须非零")
    amp = data_max - data_base / base_divisor

    n_evt = min(ch.size, amp.size)
    ch = ch[:n_evt]
    amp = amp[:n_evt]
    utc = _extract_time_axis(event_data, n_evt, fields.get("utc"))

    # crc_check: parser 默认会在事件包里写 crc_check（bool array）；如果没有就视为全部通过
    crc_raw = event_data.get(fields.get("crc", "crc_check"))
    if crc_raw is not None:
        crc_check = np.asarray(crc_raw, dtype=bool).reshape(-1)
        if crc_check.size >= n_evt:
            crc_check = crc_check[:n_evt]
        else:
            tmp = np.ones(n_evt, dtype=bool)
            tmp[: crc_check.size] = crc_check
            crc_check = tmp
    else:
        crc_check = np.ones(n_evt, dtype=bool)

    # cal_ccm: 来自 data_ccm / 65535（与 cali_format/example.ipynb 约定一致），可选
    data_ccm = np.asarray(event_data.get(fields.get("ccm", "data_ccm"), []), dtype=float).reshape(-1)
    if data_ccm.size >= n_evt:
        cal_ccm = data_ccm[:n_evt] / ccm_divisor
    else:
        cal_ccm = np.full(n_evt, np.nan, dtype=float)

    temp_by_ch: dict[int, float] = {}
    bias_by_ch: dict[int, float] = {}
    channel_meta: dict[str, dict[str, float]] = {}

    temp = np.full(n_evt, np.nan, dtype=float)
    bias = np.full(n_evt, np.nan, dtype=float)

    condition_mode = conditions.get("mode", "hk")
    if condition_mode not in {"hk", "filename", "manifest", "fixed"}:
        raise ValueError(f"未知温度/偏压来源: {condition_mode}")

    def condition_value(name: str, channel: int) -> float:
        source = conditions if condition_mode == "fixed" else observation
        value = source.get(name)
        if isinstance(value, dict):
            value = value.get(str(channel), value.get(channel))
        elif isinstance(value, (list, tuple)):
            value = value[channel] if channel < len(value) else None
        if value is None or not np.isfinite(float(value)):
            raise ValueError(f"{condition_mode} 未提供通道 {channel} 的 {name}；请补充工况映射")
        # Discovery normalizes filename/manifest observations into °C and V.
        # Fixed values are also expressed in those physical units.
        return float(value)

    tail_samples = int(transform.get("tail_samples", 60))
    if tail_samples < 1:
        raise ValueError("HK 尾部平均样本数必须大于零")
    for c in range(4):
        t_name = fields.get("temp", "sipm_temp{channel}").format(channel=c, ch=c)
        v_name = fields.get("bias", "sipm_voltage{channel}").format(channel=c, ch=c)
        t_raw = np.asarray(hk_data.get(t_name, []), dtype=float).reshape(-1)
        v_raw = np.asarray(hk_data.get(v_name, []), dtype=float).reshape(-1)
        if condition_mode == "hk":
            t_c = t_raw * float(transform.get("temp_scale", 0.01)) + float(transform.get("temp_offset", -273.15))
            v_c = v_raw * float(transform.get("bias_scale", 0.001)) + float(transform.get("bias_offset", 0.0))
            if np.any(ch == c) and (not t_c.size or not v_c.size):
                raise ValueError(f"HK 缺少通道 {c} 的温度/偏压字段 {t_name!r}/{v_name!r}；请核对字段映射")
        else:
            t_c = np.asarray([condition_value("temp", c)])
            v_c = np.asarray([condition_value("bias", c)])
        t_stat = _tail_stats(t_c, n=tail_samples)
        v_stat = _tail_stats(v_c, n=tail_samples)

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
                stable_mask = v_c > float(transform.get("stable_bias_min", 20.0))
                if np.any(stable_mask):
                    first_stable = int(np.argmax(stable_mask))
                    v_stable = v_c[first_stable:]
                    if v_stable.size >= 2:
                        src = np.linspace(0.0, 1.0, v_stable.size)
                        dst = np.linspace(0.0, 1.0, ch_idx.size)
                        bias[ch_idx] = np.interp(dst, src, v_stable)
                    else:
                        bias[ch_idx] = float(v_stable[0])
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
                "channel": ch[valid].astype(int),
                "utc": utc[valid],
                "temp": temp[valid],
                "bias": bias[valid],
                "crc_check": crc_check[valid],
                "cal_ccm": cal_ccm[valid],
            }
        )
    else:
        valid = np.isfinite(amp)
        df = pd.DataFrame(
            {
                "amp": amp[valid],
                "channel": ch[valid].astype(int),
                "utc": utc[valid],
                "crc_check": crc_check[valid],
                "cal_ccm": cal_ccm[valid],
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
        "conditions_source": condition_mode,
        "crc": {
            "available": crc_raw is not None,
            "checked_events": min(int(np.asarray(crc_raw).size), n_evt) if crc_raw is not None else 0,
            "failed_events": int(np.count_nonzero(~crc_check)) if crc_raw is not None else 0,
        },
        "acquisition": {
            "profile_version": profile.get("version", 1),
            "profile_sha256": profile.get("_sha256"),
            "revision": profile.get("_revision"),
            "xml": {
                kind: {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for kind, path in xml_files.items()
            },
            "parser": settings,
            "observation": observation,
        },
    }
    return df, metadata


def to_hk_data(channel_meta: dict[str, Any]) -> dict[str, Any]:
    """将 nested channel_meta 转换为 cali_format wiki 规范的 flat hk_data。

    规范要求四个长度为 4 的平铺数值列表：
    ``{"temp": [...], "bias": [...], "temp_err": [...], "bias_err": [...]}``
    """
    return {
        "temp": [float(channel_meta[str(c)].get("temp", float("nan"))) for c in range(4)],
        "bias": [float(channel_meta[str(c)].get("bias", float("nan"))) for c in range(4)],
        "temp_err": [float(channel_meta[str(c)].get("temp_err", 0.0)) for c in range(4)],
        "bias_err": [float(channel_meta[str(c)].get("bias_err", 0.0)) for c in range(4)],
    }
