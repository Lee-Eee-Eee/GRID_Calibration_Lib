"""Bounded, fingerprinted cache for immutable interactive read intermediates."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from threading import RLock


def file_fingerprint(path: Path):
    path = Path(path).resolve()
    stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)


def _bytes(value):
    if hasattr(value, "memory_usage"):
        return int(value.memory_usage(index=True, deep=True).sum())
    if hasattr(value, "nbytes"):
        return int(value.nbytes)
    if isinstance(value, dict):
        return sum(_bytes(key) + _bytes(item) for key, item in value.items())
    if isinstance(value, (tuple, list)):
        return sum(_bytes(item) for item in value)
    if isinstance(value, (str, bytes)):
        return len(value) * (2 if isinstance(value, str) else 1)
    return 32


class ReadCache:
    """LRU whose entries are shared read-only and bounded by bytes and count.

    Loading under the lock prevents rapid same-file requests from allocating
    duplicate large frames. Callers must not cache final, editable fit records.
    """

    def __init__(self, max_bytes=128 * 1024 * 1024, max_entries=128):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self._entries = OrderedDict()
        self._lock = RLock()
        self.clear()

    def clear(self):
        with self._lock:
            self._entries.clear()
            self.bytes = self.hits = self.misses = self.evictions = 0

    def get(self, key, loader):
        with self._lock:
            if key in self._entries:
                self.hits += 1
                value, size = self._entries.pop(key)
                self._entries[key] = (value, size)
                return value
            self.misses += 1
            value = loader()
            size = _bytes(value)
            if size <= self.max_bytes:
                while self._entries and (self.bytes + size > self.max_bytes or len(self._entries) >= self.max_entries):
                    _, (_, removed) = self._entries.popitem(last=False)
                    self.bytes -= removed
                    self.evictions += 1
                self._entries[key] = (value, size)
                self.bytes += size
            return value


EC_READ_CACHE = ReadCache()
