"""Small on-demand previews for files saved by workbench operations."""

import json
from pathlib import Path


def _sample(value, depth=0):
    if depth >= 6 and isinstance(value, (dict, list)):
        return f"{len(value)} items"
    if isinstance(value, dict):
        return {key: _sample(item, depth + 1) for key, item in list(value.items())[:20]}
    if isinstance(value, list):
        return [_sample(item, depth + 1) for item in value[:4]]
    return value[:1200] if isinstance(value, str) else value


def preview(path: Path, *, relative: str, url: str):
    size = path.stat().st_size
    result = {"name": path.name, "path": relative, "url": url, "size": size}
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return {**result, "kind": "image", "image_url": url, "truncated": False}
    with path.open("rb") as stream:
        raw = stream.read(min(size, 256 * 1024))
    text = raw.decode("utf-8", errors="replace")
    if path.suffix.lower() == ".json" and len(raw) == size:
        data = json.loads(text)
        sample = _sample(data)
        snippet = json.dumps(sample, ensure_ascii=False, indent=2)
        return {**result, "kind": "json", "json": sample, "preview": sample,
                "snippet": snippet[:12000], "truncated": len(snippet) > 12000 or sample != data}
    return {**result, "kind": "json" if path.suffix.lower() == ".json" else "text",
            "snippet": text[:12000], "truncated": size > len(raw) or len(text) > 12000}
