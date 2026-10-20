"""Validate and freeze editable packet definitions without modifying source XML."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .acquisition import default_profile, validate_profile

MAX_XML_BYTES = 1024 * 1024


def presets():
    directory = Path(__file__).resolve().parents[1] / "resources" / "legacy"
    return [
        {"id": name, "label": label, "path": str(directory / (name + ".xml"))}
        for name, label in (
            ("hnu_packet", "HNU / 当前默认协议"),
            ("grid_packet", "GRID / 另一内置协议"),
            ("grid10_packet", "GRID-10B / 波形与 106-byte HK"),
        )
    ]


def xml_document(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_XML_BYTES:
        raise ValueError("XML 需为不超过 1 MiB 的文本")
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise ValueError("协议 XML 不支持 DTD 或实体定义")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"XML 语法错误: {exc}") from exc
    if not list(root):
        raise ValueError("XML 中没有数据包定义")
    return root


def read_xml(workspace, *, path=None, preset=None):
    if preset:
        item = next((item for item in presets() if item["id"] == preset), None)
        if not item:
            raise ValueError("未知 XML 预设")
        path = item["path"]
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError("请选择 XML 文件或预设")
    source = Path(path).expanduser()
    source = (Path(workspace) / source).resolve() if not source.is_absolute() else source.resolve()
    if source.suffix.lower() != ".xml" or not source.is_file():
        raise ValueError("请选择存在的 XML 文件")
    if source.stat().st_size > MAX_XML_BYTES:
        raise ValueError("XML 文件超过 1 MiB")
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"XML 文件无法以 UTF-8 读取: {source}") from exc
    root = xml_document(text)
    return {
        "path": str(source),
        "text": text,
        "tags": [item.tag for item in root],
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def prepare_profile(workspace, profile=None, payload_profile=None):
    prepared = validate_profile(profile if profile is not None else default_profile(payload_profile))
    for role in ("event", "hk"):
        section = prepared["xml"][role]
        # A path is imported once; subsequent editor contents are authoritative.
        if section.get("text") is None:
            document = read_xml(
                workspace,
                path=section.get("source_path"),
                preset=None if section.get("source_path") else "hnu_packet",
            )
            section.update(source_path=document["path"], text=document["text"])
        section.pop("snapshot_path", None)
        root = xml_document(section["text"])
        tag = prepared["parser"][role + "_tag"]
        packet = next((child for child in root if child.tag == tag), None)
        if packet is None:
            raise ValueError(f"{role} XML 中没有所选包标签 {tag}")
        try:
            length = int(packet.attrib["packet_len"])
            if length < 1 or length > 16 * 1024 * 1024:
                raise ValueError
            framing_length = 0
            for attribute in ("head", "tail"):
                if attribute == "tail" and attribute not in packet.attrib:
                    continue
                tokens = packet.attrib[attribute].split(";")
                for byte in tokens:
                    if not re.fullmatch(r"0x[0-9a-f]{1,2}", byte, re.I):
                        raise ValueError
                framing_length += len(tokens)
            if framing_length > length:
                raise ValueError
            default_evt, default_step = {
                "ft_packet": (20, 24),
                "tl_packet": (15, 16),
                "grid1x_ft_packet": (40, 12),
            }.get(tag, (1, None))
            multi_evt = prepared["parser"].get(role + "_multi_evt") or default_evt
            multi_step = prepared["parser"].get(role + "_multi_step") or default_step
            for field in packet:
                layout = {}
                for option in ("start", "size", "len"):
                    element = field.find(option)
                    if element is None or element.text is None:
                        raise ValueError
                    number = int(element.text)
                    if number < (0 if option == "start" else 1):
                        raise ValueError
                    layout[option] = number
                if field.attrib.get("endian", prepared["parser"]["endian"]) not in ("MSB", "LSB"):
                    raise ValueError
                start_node = field.find("start")
                if "repeat" in field.attrib or "vary_repeat" in start_node.attrib:
                    if multi_step is None:
                        raise ValueError
                start = layout["start"]
                if "vary_repeat" in start_node.attrib:
                    start += int(start_node.attrib["base_start"]) + multi_evt * multi_step
                stop = start + layout["size"] * layout["len"]
                if "repeat" in field.attrib:
                    stop += (multi_evt - 1) * multi_step
                if "vary_wf" not in start_node.attrib and stop > length:
                    raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{role} 包 {tag} 的 packet_len/head/tail 或字段布局无效") from exc
        fields = prepared["parser"]["fields"]
        required = [fields[key] for key in ("channel", "max", "base")] if role == "event" else []
        if role == "hk" and prepared["parser"]["conditions"]["mode"] == "hk":
            required_keys = ["temp", "bias"]
            if prepared["parser"]["transform"]["bias_mode"] != "affine":
                required_keys.append("current")
            required = [fields[key].format(channel=channel) for key in required_keys for channel in range(4)]
        names = {field.tag for field in packet}
        missing = [field for field in required if field not in names]
        if missing:
            raise ValueError(f"{role} 包 {tag} 缺少映射字段: {', '.join(missing)}")
    # Existing complete profiles are immutable descriptions. New optional
    # defaults belong to the runtime view and must not change an old preset's
    # identity when it is validated or copied into another run.
    if _complete_profile(profile):
        result = copy.deepcopy(profile)
        result["xml"] = prepared["xml"]
        return result
    return prepared


def _complete_profile(profile):
    if not isinstance(profile, dict) or not all(
        isinstance(profile.get(key), dict) for key in ("xml", "parser", "discovery")
    ):
        return False
    parser = profile["parser"]
    if not {"event_tag", "hk_tag", "endian", "fields", "transform", "conditions"} <= parser.keys():
        return False
    if not all(isinstance(parser.get(key), dict) for key in ("fields", "transform", "conditions")):
        return False
    return (
        profile.get("version") == 1
        and {"channel", "max", "base", "utc", "crc", "ccm", "temp", "bias"} <= parser["fields"].keys()
        and {
            "base_divisor",
            "ccm_divisor",
            "temp_scale",
            "temp_offset",
            "bias_scale",
            "bias_offset",
            "tail_samples",
            "stable_bias_min",
        }
        <= parser["transform"].keys()
        and {"mode", "temp", "bias", "temp_scale", "bias_scale"} <= parser["conditions"].keys()
        and {"mode", "rules", "manifest", "sources"} <= profile["discovery"].keys()
    )


def profile_digest(profile):
    value = copy.deepcopy(profile)
    for section in value.get("xml", {}).values():
        section.pop("snapshot_path", None)
    for key in list(value):
        if key.startswith("_"):
            value.pop(key)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def freeze_profile(run_root, profile, revision):
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("采集配置修订号必须为正整数")
    effective = prepare_profile(run_root, profile)
    for key in list(effective):
        if key.startswith("_"):
            effective.pop(key)
    sha = profile_digest(effective)
    directory = Path(run_root).resolve() / "acquisition" / f"{revision:04d}-{sha[:12]}"
    directory.mkdir(parents=True, exist_ok=False)
    files = []
    for role in ("event", "hk"):
        path = directory / (role + ".xml")
        data = effective["xml"][role]["text"].encode("utf-8")
        path.write_bytes(data)
        effective["xml"][role]["snapshot_path"] = str(path)
        files.append({"role": role, "path": str(path), "sha256": hashlib.sha256(data).hexdigest()})
    data = json.dumps(effective, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    path = directory / "profile.json"
    path.write_bytes(data)
    return {
        "revision": revision,
        "sha256": sha,
        "path": str(path),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "xml": files,
    }


def load_profile(reference):
    try:
        if not isinstance(reference, dict):
            raise ValueError("采集配置快照引用无效")
        for key in ("sha256", "file_sha256"):
            if not isinstance(reference.get(key), str) or not re.fullmatch(r"[a-f0-9]{64}", reference[key]):
                raise ValueError("采集配置快照 SHA-256 引用无效")
        revision = reference["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("采集配置快照修订号无效")
        path = Path(reference["path"]).resolve()
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference["file_sha256"]:
            raise ValueError("读取配置快照 SHA-256 已变化，请重新保存配置")
        profile = json.loads(data)
        validate_profile(profile)
        if profile_digest(profile) != reference["sha256"]:
            raise ValueError("采集配置内容 SHA-256 与引用不一致，请重新保存配置")
        items = reference["xml"]
        if (
            not isinstance(items, list)
            or len(items) != 2
            or not all(isinstance(item, dict) for item in items)
            or {item.get("role") for item in items} != {"event", "hk"}
        ):
            raise ValueError("采集配置必须引用唯一的 event 和 hk XML 快照")
        for item in items:
            role = item["role"]
            xml_path = Path(item["path"]).resolve()
            recorded_path = Path(profile["xml"][role]["snapshot_path"]).resolve()
            if xml_path != recorded_path or xml_path.parent != path.parent or xml_path.name != role + ".xml":
                raise ValueError(f"{role} XML 快照路径与配置记录不一致")
            xml_data = xml_path.read_bytes()
            if hashlib.sha256(xml_data).hexdigest() != item["sha256"]:
                raise ValueError(f"{role} XML 快照 SHA-256 已变化，请重新保存配置")
            if xml_data != profile["xml"][role]["text"].encode("utf-8"):
                raise ValueError(f"{role} XML 快照内容与配置记录不一致")
        profile.update(_sha256=reference["sha256"], _revision=reference["revision"])
        return profile
    except (KeyError, TypeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"采集配置快照无法恢复: {exc}") from exc
