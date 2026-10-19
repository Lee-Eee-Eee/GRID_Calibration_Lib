"""Editable, versioned descriptions of raw packet inputs.

This module only pairs files and describes observations. Packet decoding and
calibration remain in the existing library processors. The returned paths refer
to read-only inputs; callers snapshot profiles/XML before starting a job.
"""

from __future__ import annotations

import copy
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

KINDS = ("tb", "xray", "source", "background")


def default_profile(profile: str | None = None) -> dict:
    """Return an independent JSON-serializable preset for current GRID packets."""
    return {
        "version": 1,
        "xml": {
            "event": {"source_path": None, "text": None},
            "hk": {"source_path": None, "text": None},
        },
        "parser": {
            "event_tag": "grid1x_ft_packet",
            "hk_tag": "hk_packet",
            "endian": "MSB",
            "event_multi_evt": None,
            "event_multi_step": None,
            "hk_multi_evt": None,
            "hk_multi_step": None,
            "fields": {
                "channel": "channel_n",
                "max": "data_max",
                "base": "data_base",
                "utc": ["utc", "utc_time", "timestamp"],
                "crc": "crc_check",
                "ccm": "data_ccm",
                "temp": "sipm_temp{channel}",
                "bias": "sipm_voltage{channel}",
            },
            "transform": {
                "base_divisor": 4.0,
                "ccm_divisor": 65535.0,
                "temp_scale": 0.01,
                "temp_offset": -273.15,
                "bias_scale": 0.001,
                "bias_offset": 0.0,
                "tail_samples": 60,
                "stable_bias_min": 20.0,
            },
            "conditions": {
                "mode": "hk",
                "temp": None,
                "bias": None,
                "temp_scale": 1.0,
                "bias_scale": 0.1,
            },
        },
        "discovery": {
            "mode": "rules",
            "rules": [
                {
                    "kind": "tb",
                    "directory": "Temp_Vbias",
                    "event_regex": r"^(?P<key>.+?)[-_]\d+\.event\.dat$",
                    "hk_regex": r"^(?P<key>.+?)[-_]ecu_\d+\.hk$",
                    "metadata_regex": r"^(?P<temp>m?-?\d+(?:\.\d+)?)C?[-_](?P<bias>\d+(?:\.\d+)?)[-_]",
                    "prefer_suffix": "",
                },
                {
                    "kind": "xray",
                    "directory": "X" if profile == "15B" else "X_new",
                    "event_regex": r"^(?P<energy>\d+(?:\.\d+)?)-ch(?P<channel>\d).+\.event\.dat(?:\.cut)?$",
                    "hk_regex": r"^(?P<energy>\d+(?:\.\d+)?)-ch(?P<channel>\d).*-ecu_.+\.hk(?:\.cut)?$",
                    "metadata_regex": None,
                    "prefer_suffix": ".cut",
                },
                {
                    "kind": "source",
                    "directory": "source",
                    "event_regex": r"^(?P<key>.+?)[_-]\d+\.event\.dat$",
                    "hk_regex": r"^(?P<key>.+?)[_-]ecu[_-]\d+\.hk$",
                    "metadata_regex": r"^(?P<source>[^_-]+)[_-]\s*(?P<exposure>\d+(?:\.\d+)?)\s*(?P<unit>[mh])(?:[_-]|$)",
                    "prefer_suffix": "",
                },
            ],
            "manifest": [],
            "sources": [
                {"name": "Na22", "aliases": ["na22", "Na-22"], "energies_keV": [511.0, 1274.5]},
                {"name": "Cs137", "aliases": ["cs137", "Cs-137"], "energies_keV": [661.7]},
                {"name": "Co60", "aliases": ["co60", "Co-60"], "energies_keV": [1173.2, 1332.5]},
            ],
        },
    }


def _merge(base: dict, updates: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _number(value, label: str, *, positive=False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} 必须是有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是有限数值") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{label} 必须是{'正的' if positive else ''}有限数值")
    return number


def _name(value, label: str) -> str:
    value = str(value or "").strip()
    if not value or any(c in value for c in '<>:"/\\|?*') or value in (".", ".."):
        raise ValueError(f"{label} 不能为空或包含路径字符")
    if any(ord(c) < 32 for c in value) or value.endswith((".", " ")):
        raise ValueError(f"{label} 含无效字符")
    if len(value) > 200 or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", value):
        raise ValueError(f"{label} 过长或为系统保留文件名")
    return value


def _energies(values, label: str) -> list[float]:
    if not isinstance(values, list) or not values:
        raise ValueError(f"{label} 必须为非空能线数值数组")
    result = [_number(v, label, positive=True) for v in values]
    if len(result) != len(set(result)):
        raise ValueError(f"{label} 不能包含重复能线")
    return result


def validate_profile(profile: dict) -> dict:
    """Validate structure and return a complete independent profile.

    XML semantics and its immutable snapshot are checked by the service. In
    particular, extra ``snapshot_path``/hash metadata is retained here.
    """
    if not isinstance(profile, dict):
        raise ValueError("采集配置必须为 JSON 对象")
    normalized = _merge(default_profile(), profile)
    if normalized.get("version") != 1:
        raise ValueError("不支持的采集配置版本；当前为 1")
    for section in ("xml", "parser", "discovery"):
        if not isinstance(normalized.get(section), dict):
            raise ValueError(f"{section} 必须为对象")
    for kind in ("event", "hk"):
        xml = normalized["xml"].get(kind)
        if not isinstance(xml, dict):
            raise ValueError(f"xml.{kind} 必须为对象")
        for key in ("text", "source_path", "snapshot_path"):
            if xml.get(key) is not None and not isinstance(xml[key], str):
                raise ValueError(f"xml.{kind}.{key} 必须为文本")
        if xml.get("text") and len(xml["text"].encode("utf-8")) > 1024 * 1024:
            raise ValueError("XML 内容不能超过 1 MiB")
    parser = normalized["parser"]
    if parser.get("endian") not in ("MSB", "LSB"):
        raise ValueError("字节序 endian 只能为 MSB 或 LSB")
    for kind in ("event", "hk"):
        if not isinstance(parser.get(f"{kind}_tag"), str) or not parser[f"{kind}_tag"].strip():
            raise ValueError(f"{kind}_tag 不能为空")
        for option in ("multi_evt", "multi_step"):
            value = parser.get(f"{kind}_{option}")
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{kind}_{option} 必须为空或正整数")
    fields = parser.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("parser.fields 必须为对象")
    for key in ("channel", "max", "base", "temp", "bias"):
        if not isinstance(fields.get(key), str) or not fields[key].strip():
            raise ValueError(f"字段映射 {key} 不能为空")
    for key in ("temp", "bias"):
        try:
            fields[key].format(channel=0)
        except (KeyError, ValueError, IndexError) as exc:
            raise ValueError(f"字段映射 {key} 仅支持 {{channel}} 占位符") from exc
    if not isinstance(fields.get("utc"), list) or not all(isinstance(v, str) for v in fields["utc"]):
        raise ValueError("utc 字段映射必须为字符串数组")
    for key in ("crc", "ccm"):
        if fields.get(key) is not None and not isinstance(fields[key], str):
            raise ValueError(f"{key} 字段映射必须为文本或空")
    transform = parser.get("transform")
    if not isinstance(transform, dict):
        raise ValueError("parser.transform 必须为对象")
    for key, value in transform.items():
        transform[key] = _number(value, key, positive=key in ("base_divisor", "ccm_divisor"))
    tail = transform.get("tail_samples")
    if int(tail) != tail or tail < 1 or tail > 1000000:
        raise ValueError("tail_samples 必须为 1–1000000 的整数")
    transform["tail_samples"] = int(tail)
    conditions = parser.get("conditions")
    if not isinstance(conditions, dict) or conditions.get("mode") not in (
        "hk",
        "filename",
        "manifest",
        "fixed",
    ):
        raise ValueError("温度偏压读取模式须为 hk / filename / manifest / fixed")
    for key in ("temp_scale", "bias_scale"):
        conditions[key] = _number(conditions[key], key)
    for key in ("temp", "bias"):
        if conditions.get(key) is not None:
            conditions[key] = _number(conditions[key], key)
        elif conditions["mode"] == "fixed":
            raise ValueError("固定工况模式必须提供 temp 和 bias（°C / V）")
    discovery = normalized["discovery"]
    if discovery.get("mode") not in ("rules", "manifest"):
        raise ValueError("文件读取模式须为 rules 或 manifest")
    for key in ("rules", "manifest", "sources"):
        if not isinstance(discovery.get(key), list):
            raise ValueError(f"discovery.{key} 必须为数组")
    if len(discovery["manifest"]) > 100000:
        raise ValueError("映射清单不能超过 100000 行")
    for rule in discovery["rules"]:
        if not isinstance(rule, dict) or rule.get("kind") not in KINDS:
            raise ValueError("规则 kind 须为 tb / xray / source / background")
        if not isinstance(rule.get("directory"), str):
            raise ValueError("规则 directory 必须为目录文本")
        for key in ("event_regex", "hk_regex", "metadata_regex"):
            value = rule.get(key)
            if key == "metadata_regex" and not value:
                continue
            if not isinstance(value, str) or not value or len(value) > 4000:
                raise ValueError(f"{key} 必须为长度 1–4000 的正则表达式")
            try:
                expression = re.compile(value, re.I)
            except re.error as exc:
                raise ValueError(f"{key} 正则表达式无效: {exc}") from exc
            if key != "metadata_regex" and not (
                "key" in expression.groupindex
                or ("energy" in expression.groupindex and {"channel", "ch"} & expression.groupindex.keys())
                or ("source" in expression.groupindex and "exposure" in expression.groupindex)
            ):
                raise ValueError(f"{key} 需要命名组 key、energy+channel 或 source+exposure 用于唯一配对")
        if not isinstance(rule.get("prefer_suffix", ""), str):
            raise ValueError("prefer_suffix 必须为文本")
    aliases = set()
    for source in discovery["sources"]:
        if not isinstance(source, dict):
            raise ValueError("源规格必须为对象")
        source["name"] = _name(source.get("name"), "源名称")
        if not isinstance(source.get("aliases", []), list):
            raise ValueError("源别名必须为数组")
        energies = source.get("energies_keV")
        if not isinstance(energies, list) or not energies:
            raise ValueError(f"源 {source['name']} 必须显式提供能线 energies_keV")
        source["energies_keV"] = _energies(energies, "能线 keV")
        own_aliases = {_signature(source["name"]), *(_signature(v) for v in source.get("aliases", []))}
        if aliases & own_aliases:
            raise ValueError("源名称或别名重复，无法唯一识别")
        aliases.update(own_aliases)
    return normalized


def _signature(value) -> str:
    return re.sub(r"[^\w]", "", str(value).casefold())


def _float_label(value, label):
    value = str(value).strip()
    if value.lower().startswith("m"):
        value = "-" + value[1:]
    return _number(value, label)


def _pair_key(groups: dict) -> str:
    if groups.get("key"):
        return _signature(groups["key"])
    if groups.get("energy") and groups.get("channel", groups.get("ch")) is not None:
        return f"{float(groups['energy']):g}:ch{int(groups.get('channel', groups.get('ch')))}"
    return f"{_signature(groups.get('source'))}:{float(groups.get('exposure')):g}:{groups.get('unit', 'm').lower()}"


def _normal_row(row: dict, root: Path, profile: dict, *, from_manifest=False) -> dict:
    if not isinstance(row, dict) or row.get("kind") not in KINDS:
        raise ValueError("映射行 kind 须为 tb / xray / source / background")
    result = copy.deepcopy(row)
    for role in ("event", "hk"):
        value = result.get(role)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"映射行缺少 {role} 文件")
        path = Path(value).expanduser()
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_file():
            raise ValueError(f"{role} 文件不存在: {path}")
        if path.stat().st_size == 0:
            raise ValueError(f"{role} 文件为空: {path.name}")
        result[role] = str(path)
    if result["event"] == result["hk"]:
        raise ValueError("event 和 hk 不能使用同一个文件")
    kind = result["kind"]
    conditions = profile["parser"]["conditions"]
    if conditions["mode"] == "fixed":
        result["temp"], result["bias"] = conditions["temp"], conditions["bias"]
    elif conditions["mode"] in ("filename", "manifest"):
        if conditions["mode"] == "manifest" and not from_manifest:
            raise ValueError("manifest 工况模式需要显式文件映射清单")
        for key in ("temp", "bias"):
            if result.get(key) is None:
                raise ValueError(f"{conditions['mode']} 工况模式缺少 {key}")
            # Explicit manifest/fixed observations are already in °C and V.
            scale = 1.0 if from_manifest else conditions[f"{key}_scale"]
            result[key] = _float_label(result[key], key) * scale
    else:
        # Nominal filename values remain visible for review but do not replace HK.
        for key in ("temp", "bias"):
            if result.get(key) is not None:
                result[key] = _float_label(result[key], key) * (
                    1.0 if from_manifest else conditions[f"{key}_scale"]
                )
    if kind == "xray":
        result["energy_keV"] = _number(
            result.get("energy_keV", result.get("energy")), "X 射线能量", positive=True
        )
        channel = _number(result.get("channel", result.get("ch")), "通道")
        if channel not in range(4):
            raise ValueError("通道必须为 0、1、2 或 3")
        result["channel"] = int(channel)
        canonical = f"{result['energy_keV']:g}keV_ch{result['channel']}"
    elif kind in ("source", "background"):
        source_name = result.get("source", "bkg" if kind == "background" else "")
        signature = _signature(source_name)
        if signature in ("bkg", "background", "本底"):
            result["kind"], result["source"] = "background", "bkg"
            result["peak_energies_keV"] = []
        else:
            source = next(
                (
                    s
                    for s in profile["discovery"]["sources"]
                    if signature in {_signature(s["name"]), *(_signature(v) for v in s.get("aliases", []))}
                ),
                None,
            )
            energies = result.get("peak_energies_keV")
            if source:
                result["source"] = source["name"]
                energies = energies or source["energies_keV"]
            elif not energies:
                raise ValueError(f"未知发射源 {source_name!r}；请在源目录或映射行配置能线")
            else:
                result["source"] = _name(source_name, "源名称")
            result["peak_energies_keV"] = _energies(energies, "发射源能线")
        exposure = result.get("exposure_minutes")
        if exposure is None:
            exposure = _number(result.get("exposure"), "曝光时间", positive=True)
            unit = str(result.get("unit", "m")).casefold()
            if unit not in ("m", "min", "minute", "minutes", "h", "hour", "hours", "s", "sec"):
                raise ValueError(f"未知曝光时间单位 {unit}")
            exposure *= 60 if unit in ("h", "hour", "hours") else 1 / 60 if unit in ("s", "sec") else 1
        result["exposure_minutes"] = _number(exposure, "曝光分钟", positive=True)
        canonical = f"src_{result['source']}_{result['exposure_minutes']:g}m"
    else:
        canonical = Path(result["event"]).name.removesuffix(".event.dat").removesuffix(".dat")
    # EC canonical names are the stable contract consumed by later processors.
    if kind != "tb" and result.get("stem") and result["stem"] != canonical:
        raise ValueError(f"EC 输出 stem 须为规范名称 {canonical}")
    result["stem"] = _name(result.get("stem") or canonical, "输出 stem")
    return result


def discover(raw_root: Path | str, profile: dict) -> dict:
    """Preview real file pairs and metadata without decoding packets or writing."""
    profile = validate_profile(profile)
    root = Path(raw_root).expanduser().resolve()
    rows, errors, unmatched, ambiguous = [], [], [], []
    if not root.is_dir():
        return {
            "rows": [],
            "errors": [f"原始数据目录不存在: {root}"],
            "unmatched": [],
            "ambiguous": [],
            "counts": {},
        }
    discovery = profile["discovery"]
    if discovery["mode"] == "manifest":
        for index, row in enumerate(discovery["manifest"]):
            try:
                rows.append(_normal_row(row, root, profile, from_manifest=True))
            except (ValueError, OSError) as exc:
                errors.append(f"映射第 {index + 1} 行: {exc}")
    else:
        for rule_index, rule in enumerate(discovery["rules"]):
            directory = Path(rule["directory"]).expanduser()
            directory = (directory if directory.is_absolute() else root / directory).resolve()
            if not directory.is_dir():
                errors.append(f"规则 {rule_index + 1} 的目录不存在: {directory}")
                continue
            maps = {"event": defaultdict(list), "hk": defaultdict(list)}
            patterns = {role: re.compile(rule[f"{role}_regex"], re.I) for role in maps}
            for path in sorted(directory.iterdir()):
                if not path.is_file():
                    continue
                matched = False
                for role, pattern in patterns.items():
                    match = pattern.match(path.name)
                    if match:
                        matched = True
                        try:
                            maps[role][_pair_key(match.groupdict())].append((path, match.groupdict()))
                        except (ValueError, TypeError) as exc:
                            errors.append(f"{path.name} 配对标识无效: {exc}")
                if not matched and path.suffix.casefold() not in (
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".svg",
                    ".pdf",
                ):
                    unmatched.append(
                        {"path": str(path), "reason": "未匹配 event/hk 规则", "rule": rule_index + 1}
                    )
            for key in sorted(set(maps["event"]) | set(maps["hk"])):
                pair, groups = {}, {}
                invalid = False
                for role in maps:
                    candidates = maps[role].get(key, [])
                    nonempty = [item for item in candidates if item[0].stat().st_size > 0]
                    empty = [item for item in candidates if item[0].stat().st_size == 0]
                    for path, _ in empty:
                        unmatched.append({"path": str(path), "reason": "文件为空", "rule": rule_index + 1})
                    suffix = rule.get("prefer_suffix", "")
                    preferred = [item for item in nonempty if suffix and item[0].name.endswith(suffix)]
                    if preferred:
                        nonempty = preferred
                    if len(nonempty) != 1:
                        invalid = True
                        if len(nonempty) > 1:
                            ambiguous.append(
                                {
                                    "key": key,
                                    "role": role,
                                    "paths": [str(p) for p, _ in nonempty],
                                    "rule": rule_index + 1,
                                }
                            )
                        else:
                            for path, _ in maps["hk" if role == "event" else "event"].get(key, []):
                                unmatched.append(
                                    {
                                        "path": str(path),
                                        "reason": f"缺少非空 {role} 配对",
                                        "rule": rule_index + 1,
                                    }
                                )
                    else:
                        pair[role] = str(nonempty[0][0])
                        groups.update({k: v for k, v in nonempty[0][1].items() if v is not None})
                if invalid:
                    continue
                if rule.get("metadata_regex"):
                    metadata = re.match(rule["metadata_regex"], Path(pair["event"]).name, re.I)
                    if metadata:
                        groups.update({k: v for k, v in metadata.groupdict().items() if v is not None})
                try:
                    rows.append(_normal_row({"kind": rule["kind"], **groups, **pair}, root, profile))
                except (ValueError, OSError) as exc:
                    errors.append(f"{Path(pair['event']).name}: {exc}")
    # Repeated rules/manifest rows must never silently replace an output or reuse
    # an event/hk under conflicting observation metadata.
    conflicts = set()
    background_rows = [index for index, row in enumerate(rows) if row["kind"] == "background"]
    if len(background_rows) > 1:
        conflicts.update(background_rows)
        errors.append(
            "发现多个发射源本底；当前处理器需要唯一全局本底，请使用映射清单明确选择一个 background 文件对"
        )
    for key_fn, description in (
        (lambda r: ("tb" if r["kind"] == "tb" else "ec", r["stem"]), "输出 stem 重复"),
        (lambda r: r["event"], "event 被重复引用"),
        (lambda r: r["hk"], "hk 被重复引用"),
    ):
        indexed = defaultdict(list)
        for index, row in enumerate(rows):
            indexed[key_fn(row)].append(index)
        for key, indices in indexed.items():
            if len(indices) > 1:
                conflicts.update(indices)
                errors.append(f"{description}: {key}")
    rows = [row for index, row in enumerate(rows) if index not in conflicts]
    if ambiguous:
        errors.append(f"有 {len(ambiguous)} 组 event/hk 文件无法唯一配对；请修改规则或使用显式映射清单")
    counts = dict(Counter(row["kind"] for row in rows))
    return {"rows": rows, "errors": errors, "unmatched": unmatched, "ambiguous": ambiguous, "counts": counts}
