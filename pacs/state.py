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
