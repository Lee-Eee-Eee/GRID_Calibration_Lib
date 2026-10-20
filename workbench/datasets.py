"""Discover read-only calibration inputs without fixing the UI to two payloads.

The public dataset paths identify one payload directory each. A user can pass a
parent directory to ``scan``; its children are discovered and paired by name.
Only the small catalog under runs is written, never the selected inputs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

RAW_FOLDERS = ("Temp_Vbias", "X", "X_new", "source", "tb_data", "x_data", "src_data")


def validate_payload(value: str) -> str:
    value = str(value).strip()
    if (
        not value
        or len(value) > 80
        or value in (".", "..")
        or any(character in value for character in '<>:"/\\|?*')
        or any(ord(character) < 32 for character in value)
        or value.endswith((".", " "))
        or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", value)
    ):
        raise ValueError("载荷编号须为 1–80 个字符，不能包含路径分隔符或系统保留文件名")
    return value


def _key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", str(value).lower())
    return {
        "02": "14b",
        "grid02": "14b",
        "grid14b": "14b",
        "03": "15b",
        "grid03": "15b",
        "grid15b": "15b",
        "grid10brawdata": "10b",
    }.get(normalized, normalized)


def _label(path: Path) -> str:
    return {"10b": "10B", "14b": "14B", "15b": "15B"}.get(_key(path.name), path.name)


class DatasetCatalog:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace / "runs" / "datasets.json"

    def _path(self, value, *, required=False):
        if value is None or not str(value).strip():
            if required:
                raise ValueError("未选择目录")
            return None
        path = Path(os.path.expandvars(str(value).strip())).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if not path.is_dir():
            raise ValueError(f"目录不存在或不是文件夹: {path}")
        return path

    @staticmethod
    def _is_raw(path):
        return any((path / name).is_dir() for name in RAW_FOLDERS)

    @staticmethod
    def _is_product(path):
        return (path / "tb").is_dir() or (path / "ec").is_dir()

    def _raw_sources(self, path):
        if path is None:
            return []
        if path.name in RAW_FOLDERS and self._is_raw(path.parent):
            return [path.parent]
        if self._is_raw(path):
            return [path]
        return sorted(
            (child for child in path.iterdir() if child.is_dir() and self._is_raw(child)),
            key=lambda p: p.name,
        )

    def _product_sources(self, path):
        if path is None:
            return []
        if self._is_product(path):
            return [path]
        # A previously saved run can be selected directly, as can its products/.
        if (path / "products").is_dir():
            return self._product_sources(path / "products")
        return sorted(
            (child for child in path.iterdir() if child.is_dir() and self._is_product(child)),
            key=lambda p: p.name,
        )

    def defaults(self):
        raw = self.workspace / "14、15B标定数据"
        if not raw.is_dir():
            raw = next(
                (
                    child
                    for child in sorted(self.workspace.iterdir())
                    if child.is_dir() and self._raw_sources(child)
                ),
                raw,
            )
        products = self.workspace / "products"
        return {
            "raw_dir": str(raw) if raw.is_dir() else None,
            "product_dir": str(products) if products.is_dir() else None,
        }

    def resolve(self, dataset: dict) -> dict:
        """Return a normalized, validated payload definition for a saved run."""
        if not isinstance(dataset, dict):
            raise ValueError("数据集定义须为对象")
        raw = self._path(dataset.get("raw_dir"))
        product = self._path(dataset.get("product_dir"))
        source_name = str(dataset.get("source_payload") or dataset.get("payload") or "")
        raw_candidates = self._raw_sources(raw)
        product_candidates = self._product_sources(product)
        if raw and not raw_candidates:
            # Explicitly selected future layouts are configured in the reading
            # profile; discovery need not assume today's subdirectory names.
            pass
        elif raw and len(raw_candidates) != 1:
            matches = [item for item in raw_candidates if _key(item.name) == _key(source_name)]
            if len(matches) != 1:
                raise ValueError(
                    "原始数据目录含多个载荷或未发现 Temp_Vbias/X/X_new/source，请选择其中一个载荷目录"
                )
            raw = matches[0]
        elif raw_candidates:
            raw = raw_candidates[0]
        if product and len(product_candidates) != 1:
            matches = [item for item in product_candidates if _key(item.name) == _key(source_name)]
            if len(matches) != 1:
                raise ValueError("产品目录含多个载荷或未发现 tb/ec，请选择其中一个载荷目录")
            product = matches[0]
        elif product_candidates:
            product = product_candidates[0]
        if raw is None and product is None:
            raise ValueError("至少需要选择原始数据目录或产品目录")
        payload = validate_payload(dataset.get("payload") or _label(raw or product))
        source_payload = product.name if product else str(dataset.get("source_payload") or payload)
        profile = dataset.get("profile")
        if profile not in ("10B", "14B", "15B"):
            profile = next(
                (
                    {"10b": "10B", "14b": "14B", "15b": "15B"}[_key(value)]
                    for value in ((raw.name if raw else ""), source_payload, payload)
                    if _key(value) in ("10b", "14b", "15b")
                ),
                None,
            )
        record = {
            "payload": payload,
            "label": payload,
            "raw_dir": str(raw) if raw else None,
            "product_dir": str(product) if product else None,
            "source_payload": source_payload,
            "profile": profile,
        }
        # Preset selection follows the directory the user explicitly selected;
        # an automatically paired default directory must not override it.
        roles = dataset.get("selection_roles")
        if roles is not None:
            if (
                not isinstance(roles, list)
                or not roles
                or any(role not in ("raw", "product") for role in roles)
            ):
                raise ValueError("目录选择来源无效")
            record["selection_roles"] = sorted(set(roles))
        identity = json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return {"id": hashlib.sha256(identity).hexdigest()[:16], **record}

    def scan(self, raw_dir=None, product_dir=None, payload=None) -> dict:
        defaults = self.defaults()
        raw_root = self._path(raw_dir or defaults["raw_dir"])
        product_root = self._path(product_dir or defaults["product_dir"])
        raws = self._raw_sources(raw_root)
        products = self._product_sources(product_root)
        if raw_dir and not raws:
            raws = [raw_root]
        if product_dir and not products:
            raise ValueError(
                "产品目录中未发现 tb/ec 分层产物；可选择 products 根目录、载荷目录或已有试算目录"
            )
        pairs = []
        if raw_dir and len(raws) == 1 and product_dir and len(products) == 1:
            pairs = [(raws[0], products[0])]
        elif raw_dir and not product_dir:
            pairs = [
                (raw, next((p for p in products if _key(p.name) == _key(raw.name)), None)) for raw in raws
            ]
        elif product_dir and not raw_dir:
            pairs = [
                (next((r for r in raws if _key(r.name) == _key(product.name)), None), product)
                for product in products
            ]
        else:
            used = set()
            for raw in raws:
                product = next((p for p in products if _key(p.name) == _key(raw.name)), None)
                pairs.append((raw, product))
                if product:
                    used.add(product)
            pairs.extend((None, product) for product in products if product not in used)
        if payload and len(pairs) > 1:
            raise ValueError("父目录发现多个载荷；请先选定一个数据集，再编辑载荷编号")
        datasets = [
            self.resolve(
                {
                    "raw_dir": str(raw) if raw else None,
                    "product_dir": str(product) if product else None,
                    "payload": payload or _label(raw or product),
                    **(
                        {"selection_roles": ["raw" if raw_dir else "product"]}
                        if bool(raw_dir) != bool(product_dir)
                        else {}
                    ),
                }
            )
            for raw, product in pairs
        ]
        previous = {}
        if self.path.exists():
            try:
                previous = {
                    item["id"]: item
                    for item in json.loads(self.path.read_text(encoding="utf-8")).get("datasets", [])
                }
            except (ValueError, KeyError, TypeError):
                pass
        previous.update((item["id"], item) for item in datasets)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"datasets": list(previous.values()), "defaults": defaults}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)
        return {"datasets": datasets, "defaults": defaults}
