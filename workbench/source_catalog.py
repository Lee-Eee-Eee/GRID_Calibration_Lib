"""Read-only, metadata-only source inventories persisted beneath workspace/runs.

Indexing is an explicit action. Reading a saved inventory never enumerates or
opens its source directory. Packet/spectrum content is not read by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_type(name):
    lower = name.lower()
    if lower.endswith((".event.dat", ".event.dat.cut")):
        return "event_packets"
    if lower.endswith((".hk", ".hk.cut")):
        return "housekeeping"
    return {
        ".dat": "dat",
        ".bin": "binary",
        ".json": "json",
        ".xml": "xml",
        ".parquet": "parquet",
        ".cnf": "cnf_spectrum",
        ".tka": "tka_spectrum",
        ".csv": "csv",
        ".txt": "text",
        ".pdf": "pdf",
        ".md": "documentation",
        ".py": "script",
        ".ipynb": "notebook",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".svg": "image",
        ".zip": "archive",
        ".7z": "archive",
    }.get(Path(lower).suffix, "other")


def _payload(root_name, relative):
    # Folder names are labels only; packet compatibility is determined later
    # by the selected XML/reading preset, never inferred from this inventory.
    for part in (root_name, *Path(relative).parts[:-1]):
        match = re.fullmatch(r"(?:GRID[-_]?)?(\d{1,3}[AB])(?:[_-].*)?", part, re.I)
        if match:
            return match.group(1).upper()
        if part.lower() in ("02", "grid02", "grid-02"):
            return "14B"
        if part.lower() in ("03", "grid03", "grid-03"):
            return "15B"
    return "unassigned"


def _link(info):
    # Junctions/reparse directories on Windows must not enter the traversal.
    return stat.S_ISLNK(info.st_mode) or (
        stat.S_ISDIR(info.st_mode) and getattr(info, "st_file_attributes", 0) & 0x400
    )


def _signature(info):
    # DirEntry.stat on Windows can return zero dev/inode while Path.lstat
    # reports real values for the same unchanged file.
    return info.st_size, info.st_mtime_ns


def _mtime(ns):
    return datetime.fromtimestamp(ns / 1_000_000_000, timezone.utc).isoformat(timespec="microseconds")


class SourceCatalog:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.directory = self.workspace / "runs" / "_sources"
        self.lock = threading.RLock()

    def _index_path(self, source_id):
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-f0-9]{20}", source_id):
            raise ValueError("数据源索引编号无效")
        return self.directory / source_id / "index.json"

    @staticmethod
    def _write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _summary(index):
        keys = (
            "id",
            "label",
            "root",
            "indexed_at",
            "scan_started_at",
            "duration_seconds",
            "file_count",
            "total_bytes",
            "directory_count",
            "stats",
            "status",
            "metadata_only",
            "content_hashed",
            "previous_indexed_at",
            "changes",
            "skipped_counts",
        )
        return {
            **{key: index[key] for key in keys},
            "errors_count": len(index["errors"]),
            "unstable_count": len(index["unstable"]),
        }

    def get(self, source_id):
        """Return saved JSON, including when the source is now unavailable."""
        with self.lock:
            path = self._index_path(source_id)
            if not path.is_file():
                raise FileNotFoundError("数据源索引不存在，请先建立索引")
            result = json.loads(path.read_text(encoding="utf-8"))
            if result.get("id") != source_id or result.get("schema_version") != 1:
                raise ValueError("保存的数据源索引无效，请重新建立索引")
            return result

    def list(self):
        """Read saved summaries; this method performs no source I/O."""
        with self.lock:
            sources, errors = [], []
            for path in sorted(self.directory.glob("*/index.json")):
                try:
                    summary_path = path.with_name("summary.json")
                    summary = (
                        json.loads(summary_path.read_text(encoding="utf-8"))
                        if summary_path.is_file()
                        else self._summary(self.get(path.parent.name))
                    )
                    sources.append(summary)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors.append({"id": path.parent.name, "message": str(exc)})
            return {"sources": sources, "errors": errors}

    def _root(self, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("请选择需要建立索引的数据源目录")
        path = Path(os.path.expandvars(value.strip())).expanduser()
        path = self.workspace / path if not path.is_absolute() else path
        try:
            info = path.lstat()
            if _link(info):
                raise ValueError("数据源根目录不能为符号链接或目录联接")
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("数据源路径不是目录")
            path = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"无法访问数据源目录: {path}: {exc}") from exc
        # Selecting a parent of the index store would recursively index this
        # tool's own outputs on each refresh and grow the inventory indefinitely.
        if self.directory.is_relative_to(path) or path.is_relative_to(self.directory):
            raise ValueError("请选择具体数据目录，数据源不能包含工作台 runs/_sources 索引目录")
        return path

    @staticmethod
    def _changes(previous, files):
        old = {item["path"]: item for item in previous.get("files", [])} if previous else {}
        current = {item["path"]: item for item in files}
        added = sorted(current.keys() - old.keys())
        removed = sorted(old.keys() - current.keys())
        modified = [
            {
                "path": name,
                "previous_size": old[name]["size"],
                "size": current[name]["size"],
                "previous_mtime_ns": old[name]["mtime_ns"],
                "mtime_ns": current[name]["mtime_ns"],
            }
            for name in sorted(old.keys() & current.keys())
            if (old[name]["size"], old[name]["mtime_ns"])
            != (current[name]["size"], current[name]["mtime_ns"])
        ]
        return {
            "baseline": previous is None,
            "added_count": len(added),
            "removed_count": len(removed),
            "modified_count": len(modified),
            "added": added,
            "removed": removed,
            "modified": modified,
            "comparison": "size_and_mtime_ns",
        }

    def index(self, root, label=None):
        with self.lock:
            source = self._root(root)
            if label is not None and (
                not isinstance(label, str) or not label.strip() or len(label.strip()) > 120
            ):
                raise ValueError("数据源名称须为 1–120 个字符")
            source_id = hashlib.sha256(os.path.normcase(str(source)).encode("utf-8")).hexdigest()[:20]
            index_path = self._index_path(source_id)
            previous = self.get(source_id) if index_path.is_file() else None
            started_at, started = _now(), time.monotonic()
            files, errors, skipped, unstable = [], [], [], []
            signatures = {}
            stack, directory_count = [source], 0

            def error(path, operation, exc):
                errors.append(
                    {
                        "path": path.relative_to(source).as_posix(),
                        "operation": operation,
                        "message": str(exc),
                        "exception": type(exc).__name__,
                    }
                )

            def safe_directory(path):
                for current in (source, *reversed(path.parents)):
                    if current == source or current.is_relative_to(source):
                        if _link(current.lstat()):
                            return False
                return not _link(path.lstat())

            while stack:
                folder = stack.pop()
                try:
                    if not safe_directory(folder):
                        skipped.append(
                            {"path": folder.relative_to(source).as_posix(), "reason": "link_or_junction"}
                        )
                        continue
                    with os.scandir(folder) as iterator:
                        entries = sorted(iterator, key=lambda entry: entry.name.casefold())
                    directory_count += 1
                except OSError as exc:
                    error(folder, "list_directory", exc)
                    continue
                for entry in entries:
                    path = folder / entry.name
                    relative = path.relative_to(source).as_posix()
                    if entry.name.endswith("~"):
                        skipped.append({"path": relative, "reason": "temporary_suffix"})
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if _link(info):
                            skipped.append({"path": relative, "reason": "link_or_junction"})
                        elif stat.S_ISDIR(info.st_mode):
                            stack.append(path)
                        elif stat.S_ISREG(info.st_mode):
                            files.append(
                                {
                                    "path": relative,
                                    "size": info.st_size,
                                    "mtime_ns": info.st_mtime_ns,
                                    "mtime": _mtime(info.st_mtime_ns),
                                    "type": _file_type(entry.name),
                                    "payload": _payload(source.name, relative),
                                    "directory": path.parent.relative_to(source).as_posix(),
                                    "top_directory": Path(relative).parts[0] if "/" in relative else ".",
                                    "stable": True,
                                }
                            )
                            signatures[relative] = _signature(info)
                        else:
                            skipped.append({"path": relative, "reason": "non_regular_file"})
                    except (OSError, ValueError, OverflowError) as exc:
                        error(path, "stat_file", exc)

            files.sort(key=lambda item: item["path"])
            # A second metadata pass detects acquisitions changing during a
            # potentially long directory scan without opening any packet bytes.
            for item in files:
                path = source / item["path"]
                try:
                    if not safe_directory(path.parent):
                        raise OSError("扫描期间父目录变为符号链接或目录联接")
                    info = path.lstat()
                    if _link(info) or not stat.S_ISREG(info.st_mode):
                        raise OSError("扫描期间文件被替换为链接或非普通文件")
                    if _signature(info) != signatures[item["path"]]:
                        item["stable"] = False
                        unstable.append(
                            {
                                "path": item["path"],
                                "reason": "changed_during_scan",
                                "initial_size": item["size"],
                                "initial_mtime_ns": item["mtime_ns"],
                                "observed_size": info.st_size,
                                "observed_mtime_ns": info.st_mtime_ns,
                            }
                        )
                        item.update(
                            size=info.st_size, mtime_ns=info.st_mtime_ns, mtime=_mtime(info.st_mtime_ns)
                        )
                except (OSError, ValueError, OverflowError) as exc:
                    item["stable"] = False
                    unstable.append(
                        {"path": item["path"], "reason": "unavailable_during_scan", "message": str(exc)}
                    )

            stats = {"by_type": {}, "by_payload": {}, "by_directory": {}, "by_top_directory": {}}
            for item in files:
                for group, key in (
                    ("by_type", "type"),
                    ("by_payload", "payload"),
                    ("by_directory", "directory"),
                    ("by_top_directory", "top_directory"),
                ):
                    record = stats[group].setdefault(item[key], {"files": 0, "bytes": 0})
                    record["files"] += 1
                    record["bytes"] += item["size"]
            skipped_counts = {}
            for item in skipped:
                skipped_counts[item["reason"]] = skipped_counts.get(item["reason"], 0) + 1
            result = {
                "schema_version": 1,
                "id": source_id,
                "label": label.strip() if label else previous["label"] if previous else source.name,
                "root": str(source),
                "scan_started_at": started_at,
                "indexed_at": _now(),
                "duration_seconds": round(time.monotonic() - started, 3),
                "previous_indexed_at": previous["indexed_at"] if previous else None,
                "status": "partial" if errors else "unstable" if unstable else "complete",
                "metadata_only": True,
                "content_hashed": False,
                "file_count": len(files),
                "total_bytes": sum(item["size"] for item in files),
                "directory_count": directory_count,
                "files": files,
                "stats": stats,
                "errors": errors,
                "unstable": unstable,
                "skipped": skipped,
                "skipped_counts": skipped_counts,
                "changes": self._changes(previous, files),
            }
            result["changes"]["reliable"] = not errors and not unstable
            result["changes"]["removed_is_confirmed"] = not errors
            self._write(index_path, result)
            self._write(index_path.with_name("summary.json"), self._summary(result))
            return result
