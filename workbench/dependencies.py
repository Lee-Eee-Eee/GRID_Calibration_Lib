"""Explicit, immutable TB L2 inputs for EC; independent of a run's TB fit."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from pathlib import Path


def tb_package(path: Path, origin="historical") -> dict:
    path = Path(path).resolve()
    if not path.is_file() or path.suffix.lower() != ".json":
        raise ValueError("请选择存在的 TB L2 JSON 文件")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("TB L2 JSON 文件过大")
    rows = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(rows, dict):
        rows = rows.get("fit_params")
    if not isinstance(rows, list) or len(rows) != 4:
        raise ValueError("TB L2 包须包含四个通道的参数")
    required = ("G0", "k", "V0", "b", "c")
    channels = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("TB L2 通道参数须为对象")
        channel = int(row.get("channel", index))
        if channel not in range(4) or channel in channels:
            raise ValueError("TB L2 通道须为不重复的 0–3")
        channels.add(channel)
        if any(key not in row or not math.isfinite(float(row[key])) for key in required):
            raise ValueError("TB L2 缺少有限的 G0/k/V0/b/c 参数")
        if float(row["G0"]) <= 0:
            raise ValueError("TB L2 的 G0 须大于零")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = hashlib.sha256((str(path) + sha).encode("utf-8")).hexdigest()[:20]
    return {
        "id": identity,
        "filename": path.name,
        "sha256": sha,
        "path": str(path),
        "origin": origin,
        "label": path.name,
        "modified_at": path.stat().st_mtime,
    }


def snapshot(package: dict, run_root: Path) -> dict:
    """Freeze the selected bytes so later edits to an external file cannot alter EC."""
    source = Path(package["path"])
    target = run_root / "dependencies" / package["id"] / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(source, target)
    if hashlib.sha256(target.read_bytes()).hexdigest() != package["sha256"]:
        raise ValueError("TB L2 快照校验失败，请重新选择文件")
    frozen = {**package, "path": str(target), "source_path": package.get("source_path", str(source))}
    (target.parent / "_package.json").write_text(
        json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return frozen
