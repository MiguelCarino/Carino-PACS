"""Persistent per-file send state so restarts don't re-forward everything.

Keyed by absolute path; each entry remembers the file's size+mtime (to detect
that a same-named file was replaced with new content) and which destinations
have already accepted it.
"""

from __future__ import annotations

import json
import os
import threading


class SendState:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
            except (OSError, ValueError):
                self._data = {}

    def get(self, path: str, size: int, mtime: float) -> dict:
        """Return the entry for `path`, resetting it if the file changed."""
        key = os.path.abspath(path)
        with self._lock:
            e = self._data.get(key)
            if not e or e.get("size") != size or e.get("mtime") != mtime:
                e = {"sent": [], "size": size, "mtime": mtime}
                self._data[key] = e
                self._dirty = True
            return e

    def peek(self, path: str) -> dict | None:
        """Return the existing entry for `path` without creating one (read-only)."""
        with self._lock:
            return self._data.get(os.path.abspath(path))

    def put(self, path: str, entry: dict) -> None:
        with self._lock:
            self._data[os.path.abspath(path)] = entry
            self._dirty = True

    def drop(self, path: str) -> None:
        with self._lock:
            if self._data.pop(os.path.abspath(path), None) is not None:
                self._dirty = True

    def all_entries(self) -> dict:
        """A shallow copy of every (path -> entry) pair, for read-only scans
        (the 'stuck sends' view). Entries are copied so callers can't mutate
        state without going through put()."""
        import copy
        with self._lock:
            return {k: copy.deepcopy(v) for k, v in self._data.items()}

    def clear_backoff(self, dest_names=None) -> int:
        """Zero the retry-backoff timer on failing destinations so the next
        watcher pass attempts them immediately. `dest_names` limits it to those
        destinations (None = all). Returns how many files were nudged."""
        touched = 0
        with self._lock:
            for e in self._data.values():
                fails = e.get("fail") or {}
                hit = False
                for name, f in fails.items():
                    if dest_names is None or name in dest_names:
                        if f.get("next_try", 0):
                            f["next_try"] = 0
                            hit = True
                if hit:
                    touched += 1
                    self._dirty = True
        return touched

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh)
                os.replace(tmp, self.path)
                self._dirty = False
            except OSError:
                pass
