"""Copy calibration inputs with collision checks and a verified SHA-256 manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_EXTENSIONS = {".json", ".xml", ".dat", ".hk", ".bin", ".txt", ".csv", ".tka", ".cnf", ".xlsx"}
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def migrate_sources(sources: dict[str, Path], destination: Path) -> dict:
    """Copy without replacing inputs; reruns verify and reuse identical files.

    Every destination collision is checked before the first copy. Source trees
    are never modified. A source mutation during copying raises an error.
    """
    destination = destination.resolve()
    records, ignored = [], []
    for label, source_path in sorted(sources.items()):
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label)
                or label.endswith(".") or label.split(".")[0].upper() in WINDOWS_RESERVED):
            raise ValueError(f"Invalid source label: {label!r}")
        source = source_path.resolve(strict=True)
        target = destination / label
        if not source.is_dir():
            raise ValueError(f"Source must be a directory: {source}")
        if source == target or source in target.parents or target in source.parents:
            raise ValueError("Source and destination trees must not overlap")
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"Linked inputs require an explicit separate source: {path}")
            if not path.is_file():
                continue
            if source not in path.resolve().parents:
                raise ValueError(f"Input escapes the selected source: {path}")
            relative = path.relative_to(source)
            if path.suffix.lower() not in DATA_EXTENSIONS:
                ignored.append({"source": str(path), "reason": "extension not in calibration input allowlist"})
                continue
            output = target / relative
            if destination not in output.resolve().parents:
                raise ValueError(f"Destination escapes the selected directory: {output}")
            digest = file_sha256(path)
            existing = output.exists()
            if existing and (not output.is_file() or file_sha256(output) != digest):
                raise FileExistsError(f"Destination differs; no files were overwritten: {output}")
            records.append({
                "source_label": label, "source": str(path), "relative_path": relative.as_posix(),
                "destination": str(output), "bytes": path.stat().st_size, "sha256": digest,
                "action": "verified_existing" if existing else "copied",
            })
    needed = sum(row["bytes"] for row in records if row["action"] == "copied")
    existing_parent = destination
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    if shutil.disk_usage(existing_parent).free < needed:
        raise OSError(f"Insufficient disk space for {needed} bytes")
    for row in records:
        original, output = Path(row["source"]), Path(row["destination"])
        if row["action"] == "copied":
            output.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation protects against races with another importer.
            with original.open("rb") as incoming, output.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            shutil.copystat(original, output)
        if file_sha256(output) != row["sha256"] or file_sha256(original) != row["sha256"]:
            raise OSError(f"Input changed during migration or copy verification failed: {original}")
    grouped = defaultdict(list)
    for row in records:
        grouped[row["sha256"]].append(row["source"])
    return {
        "schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
        "destination_root": str(destination), "source_roots": {k: str(v.resolve()) for k, v in sources.items()},
        "file_count": len(records), "total_bytes": sum(row["bytes"] for row in records),
        "extensions": dict(sorted(Counter(Path(row["source"]).suffix.lower() for row in records).items())),
        "actions": dict(Counter(row["action"] for row in records)), "verified": True,
        "duplicate_content": [{"sha256": digest, "sources": paths} for digest, paths in grouped.items() if len(paths) > 1],
        "ignored": ignored, "files": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True, metavar="LABEL=PATH")
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    sources = {}
    for spec in args.source:
        label, separator, path = spec.partition("=")
        if not separator or not path or label in sources:
            parser.error("Each --source must have a unique LABEL=PATH")
        sources[label] = Path(path)
    if args.manifest.exists():
        parser.error("Manifest already exists; choose a new manifest path")
    report = migrate_sources(sources, args.destination)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: report[k] for k in ("file_count", "total_bytes", "actions", "verified")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
