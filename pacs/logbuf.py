"""A tiny thread-safe log ring buffer shared by the DICOM threads and the
web dashboard.  Every component logs through here so the UI can poll a single
stream of recent events without wiring up a real logging backend."""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone


class LogBuffer:
    def __init__(self, capacity: int = 500):
        self._lock = threading.Lock()
        self._items: "deque[dict]" = deque(maxlen=capacity)
        self._seq = 0

    def add(self, level: str, message: str, **fields) -> None:
        with self._lock:
            self._seq += 1
            self._items.append(
                {
                    "seq": self._seq,
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "epoch": int(time.time()),
                    "level": level,
                    "message": message,
                    **fields,
                }
            )

    def info(self, message: str, **f) -> None:
        self.add("info", message, **f)

    def warn(self, message: str, **f) -> None:
        self.add("warn", message, **f)

    def error(self, message: str, **f) -> None:
        self.add("error", message, **f)

    def since(self, seq: int = 0) -> list[dict]:
        """Return every entry whose seq is greater than `seq` (for UI polling)."""
        with self._lock:
            return [it for it in self._items if it["seq"] > seq]

    def tail(self, n: int = 100) -> list[dict]:
        with self._lock:
            return list(self._items)[-n:]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq
