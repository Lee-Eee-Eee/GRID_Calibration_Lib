"""Immutable payload reading presets and explicit directory associations.

Only the registry index changes. Version XML/profile snapshots are verified on
every load and copied into a run, so publishing a version cannot alter a run.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .acquisition_store import freeze_profile, load_profile, prepare_profile, profile_digest
from .datasets import _key, validate_payload


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PresetRegistry:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "runs" / "_presets"
        self.path = self.root / "index.json"

    def _read(self):
        if not self.path.exists():
            return {"schema_version": 1, "presets": [], "bindings": [], "events": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not all(
            isinstance(data.get(key), list) for key in ("presets", "bindings", "events")
        ):
            raise ValueError("载荷预设目录索引无效")
        return data

    def _write(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _directory(self, value):
        path = Path(os.path.expandvars(str(value))).expanduser()
        path = (self.workspace / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_dir():
            raise ValueError(f"预设关联目录不存在: {path}")
        return str(path)

    @staticmethod
    def _path_key(value):
        return os.path.normcase(str(Path(value).resolve()))

    @staticmethod
    def _find(data, preset_id):
        if not isinstance(preset_id, str) or not re.fullmatch(r"[a-f0-9]{24}", preset_id):
            raise ValueError("载荷预设编号无效")
        item = next((p for p in data["presets"] if p["id"] == preset_id), None)
        if item is None:
            raise ValueError("载荷预设不存在")
        return item

    @staticmethod
    def _reference(preset, version):
        return {
            "id": preset["id"],
            "name": preset["name"],
            "payload": preset["payload"],
            "version": version["version"],
            "sha256": version["sha256"],
            "created_at": version["created_at"],
        }

    def list(self, payload=None):
        data = self._read()
        result = []
        for item in data["presets"]:
            if payload and _key(item["payload"]) != _key(payload):
                continue
            record = copy.deepcopy(item)
            record["bindings"] = [b for b in data["bindings"] if b["preset_id"] == item["id"]]
            result.append(record)
        return {"presets": result}

    def get(self, preset_id, version=None):
        data = self._read()
        item = self._find(data, preset_id)
        if version is None:
            version = item["latest_version"]
        if isinstance(version, bool) or not str(version).isdigit() or int(version) < 1:
            raise ValueError("载荷预设版本须为正整数")
        selected = next((v for v in item["versions"] if v["version"] == int(version)), None)
        if selected is None:
            raise ValueError("载荷预设版本不存在")
        snapshot = selected["profile_reference"]
        if selected["sha256"] != snapshot["sha256"] or selected["version"] != snapshot["revision"]:
            raise ValueError("预设版本与读取配置快照的 SHA-256 / 版本引用不一致")
        if not Path(snapshot["path"]).resolve().is_relative_to(self.root / item["id"]):
            raise ValueError("预设快照路径超出对应预设目录")
        profile = load_profile(snapshot)
        return {
            "preset": copy.deepcopy(item),
            "reference": self._reference(item, selected),
            "profile": {k: v for k, v in profile.items() if not k.startswith("_")},
        }

    def _bindings(self, bindings):
        result = []
        for binding in bindings or []:
            if not isinstance(binding, dict) or binding.get("role") not in ("raw", "product"):
                raise ValueError("目录关联须包含 raw/product 类型及目录 path")
            if not isinstance(binding.get("path"), str) or not binding["path"].strip():
                raise ValueError("目录关联 path 不能为空")
            record = {"role": binding["role"], "path": self._directory(binding.get("path", ""))}
            if record not in result:
                result.append(record)
        return result

    def _bind(self, data, reference, bindings, reason):
        for binding in bindings:
            previous = [
                item
                for item in data["bindings"]
                if item["role"] == binding["role"]
                and self._path_key(item["path"]) == self._path_key(binding["path"])
            ]
            record = {
                **binding,
                "preset_id": reference["id"],
                "version": reference["version"],
                "sha256": reference["sha256"],
                "saved_at": _now(),
            }
            data["bindings"] = [item for item in data["bindings"] if item not in previous]
            data["bindings"].append(record)
            data["events"].append(
                {"action": "bind", "at": _now(), "reason": reason, "previous": previous, "binding": record}
            )

    def save(self, *, profile, name=None, payload=None, preset_id=None, reason=None, bindings=None):
        if not isinstance(profile, dict):
            raise ValueError("保存预设必须提供完整的 profile 配置对象")
        prepared = prepare_profile(self.workspace, profile)
        associations = self._bindings(bindings)
        previous_index = self.path.read_bytes() if self.path.exists() else None
        data = self._read()
        if preset_id:
            item = self._find(data, preset_id)
            if payload and _key(payload) != _key(item["payload"]):
                raise ValueError("新版本载荷编号须与预设一致；其他载荷请另存为")
        else:
            name = str(name or "").strip()
            if not name or len(name) > 120:
                raise ValueError("请输入 1–120 个字符的预设名称")
            if not isinstance(payload, str) or not payload.strip():
                raise ValueError("请输入预设对应的载荷编号")
            payload = validate_payload(payload)
            item = {
                "id": uuid.uuid4().hex[:24],
                "name": name,
                "payload": payload,
                "created_at": _now(),
                "latest_version": 0,
                "versions": [],
            }
            data["presets"].append(item)
        version = item["latest_version"] + 1
        snapshot_base = (self.root / item["id"] / "acquisition").resolve()
        directory = snapshot_base / f"{version:04d}-{profile_digest(prepared)[:12]}"
        existed = directory.exists()
        try:
            snapshot = freeze_profile(self.root / item["id"], prepared, version)
            record = {
                "version": version,
                "sha256": snapshot["sha256"],
                "created_at": _now(),
                "reason": str(reason or "保存载荷读取预设")[:1000],
                "profile_reference": snapshot,
            }
            item["versions"].append(record)
            item["latest_version"] = version
            reference = self._reference(item, record)
            data["events"].append(
                {"action": "publish", "at": _now(), "reference": reference, "reason": record["reason"]}
            )
            self._bind(data, reference, associations, record["reason"])
            self._write(data)
        except Exception:
            if previous_index is None:
                self.path.unlink(missing_ok=True)
            else:
                temporary = self.path.with_suffix(".json.rollback")
                temporary.write_bytes(previous_index)
                temporary.replace(self.path)
            target = directory.resolve()
            if (
                not existed
                and target.parent == snapshot_base
                and directory.is_dir()
                and not directory.is_symlink()
            ):
                shutil.rmtree(target)
            raise
        return self.get(item["id"], version)

    def bind(self, preset_id, version, bindings, reason="绑定载荷目录"):
        selected = self.get(preset_id, version)
        associations = self._bindings(bindings)
        data = self._read()
        self._bind(data, selected["reference"], associations, str(reason)[:1000])
        self._write(data)
        return selected["reference"]

    def match(self, dataset):
        data = self._read()
        candidates = []
        for role, key in (("raw", "raw_dir"), ("product", "product_dir")):
            if role not in dataset.get("selection_roles", ("raw", "product")):
                continue
            if not dataset.get(key):
                continue
            for binding in data["bindings"]:
                if binding["role"] == role and self._path_key(binding["path"]) == self._path_key(
                    dataset[key]
                ):
                    item = self._find(data, binding["preset_id"])
                    selected = next(v for v in item["versions"] if v["version"] == binding["version"])
                    if binding["sha256"] != selected["sha256"]:
                        raise ValueError("目录关联与预设版本 SHA-256 不一致，请重新关联目录")
                    candidates.append(
                        {
                            **self._reference(item, selected),
                            "matched_role": role,
                            "matched_path": binding["path"],
                        }
                    )
        origin = "directory_match"
        if not candidates:
            origin = "payload_match"
            for item in data["presets"]:
                if _key(item["payload"]) in {
                    _key(dataset.get("payload", "")),
                    _key(dataset.get("source_payload", "")),
                }:
                    selected = item["versions"][-1]
                    candidates.append(self._reference(item, selected))
        identities = {(item["id"], item["version"]) for item in candidates}
        if not identities:
            return {"status": "none", "reference": None, "candidates": [], "warnings": []}
        if len(identities) > 1:
            return {
                "status": "conflict",
                "reference": None,
                "candidates": candidates,
                "warnings": ["所选数据/产品目录或载荷匹配到多个读取预设版本，请明确选择后新建试算。"],
            }
        reference = {key: value for key, value in candidates[0].items() if not key.startswith("matched_")}
        return {
            "status": "matched",
            "reference": {**reference, "origin": origin},
            "candidates": candidates,
            "warnings": [],
        }
