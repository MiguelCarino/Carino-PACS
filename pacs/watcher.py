"""Folder watcher — the "auto-send" daemon.

Polls the configured outgoing folder on an interval, and for every stable new
DICOM file forwards it (C-STORE) to each enabled destination.  Design notes:

  * Polling (not inotify): more reliable across network shares and identical on
    every OS, at the cost of up-to `poll_interval` seconds of latency.
  * A file is only sent once it is *stable* (size unchanged between two polls
    and non-zero) so we never forward a half-written object.
  * Per-destination tracking with retry: a file is "done" only once every
    currently-enabled destination has accepted it; failures are retried on the
    next pass.  State survives restarts via a small JSON sidecar.
  * Reads the live Config each pass, so dashboard edits (new hosts, changed
    folder) take effect without a restart.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from typing import Optional

from .config import Config
from .dicomfs import is_dicom, prune_empty_dirs
from .logbuf import LogBuffer
from .scu import Destination, c_store
from .state import SendState


class FolderWatcher:
    def __init__(self, cfg: Config, log: LogBuffer):
        self.cfg = cfg
        self.log = log
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._sizes: dict[str, int] = {}          # path -> last-seen size (stability check)
        self.state = SendState(os.path.join(os.path.dirname(cfg.path), ".carinopacs_state.json"))
        self._lock = threading.Lock()
        self.sent_count = 0
        self.failed_count = 0
        self.last_activity = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pacs-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        watch = self.cfg.resolved("scu", "watch_dir")
        os.makedirs(watch, exist_ok=True)
        self.log.info(f"Watcher started on {watch}", kind="watch")
        while not self._stop.is_set():
            try:
                self._scan_once()
            except Exception as exc:
                self.log.error(f"Watcher pass failed: {exc}", kind="watch")
            interval = float(self.cfg.scu.get("poll_interval", 3) or 3)
            self._stop.wait(max(1.0, interval))
        self.log.info("Watcher stopped", kind="watch")

    def _candidates(self, watch: str, sent_dir: str) -> list[str]:
        found = []
        for root, _dirs, files in os.walk(watch):
            # never rescan files we moved into the sent archive
            if os.path.abspath(root).startswith(os.path.abspath(sent_dir)):
                continue
            for name in files:
                if name.startswith("."):
                    continue
                p = os.path.join(root, name)
                if is_dicom(p):
                    found.append(p)
        return found

    def _scan_once(self) -> None:
        scu = self.cfg.scu
        watch = self.cfg.resolved("scu", "watch_dir")
        sent_dir = self.cfg.resolved("scu", "sent_dir")
        calling_aet = scu.get("aet", "CARINOSCU")
        on_success = scu.get("on_success", "keep")
        dests = [Destination.from_dict(d) for d in self.cfg.enabled_destinations()]

        if not os.path.isdir(watch):
            return
        if not dests:
            return  # nothing to send to; leave files untouched

        # Build the client TLS context once per pass if any node uses TLS.
        tls_ctx = None
        if any(d.tls for d in dests):
            try:
                from .tlsutil import client_context
                tls_ctx = client_context(
                    verify=bool(scu.get("tls_verify", True)),
                    ca=self.cfg.resolve_path(scu.get("tls_ca", "")),
                    certfile=self.cfg.resolve_path(scu.get("tls_cert", "")),
                    keyfile=self.cfg.resolve_path(scu.get("tls_key", "")),
                )
            except Exception as exc:
                self.log.error(f"TLS config error — sends to TLS nodes will fail: {exc}", kind="send")

        want = {d.name for d in dests}

        for path in self._candidates(watch, sent_dir):
            if self._stop.is_set():
                return
            try:
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
            except OSError:
                continue

            # stability gate: only proceed when size is non-zero and steady
            prev = self._sizes.get(path)
            self._sizes[path] = size
            if size == 0 or prev != size:
                continue

            entry = self.state.get(path, size, mtime)
            todo = [d for d in dests if d.name not in entry["sent"]]
            if not todo:
                self._finalize(path, want, entry, on_success, watch, sent_dir)
                continue

            for dest in todo:
                if self._stop.is_set():
                    return
                res = c_store(dest, path, calling_aet, tls_context=tls_ctx)
                if res.ok:
                    entry["sent"].append(dest.name)
                    with self._lock:
                        self.sent_count += 1
                        self.last_activity = f"{os.path.basename(path)} -> {dest.name}"
                    self.log.info(f"Sent {os.path.basename(path)} -> {dest.name}", kind="send")
                else:
                    with self._lock:
                        self.failed_count += 1
                    self.log.warn(
                        f"Send {os.path.basename(path)} -> {dest.name}: {res.message}",
                        kind="send",
                    )
            self.state.put(path, entry)
            self._finalize(path, want, entry, on_success, watch, sent_dir)

        self.state.save()

    def _finalize(self, path, want, entry, on_success, watch, sent_dir) -> None:
        """If every enabled destination has the file, apply the on_success action."""
        if not want.issubset(set(entry["sent"])):
            return
        if on_success == "delete":
            try:
                os.remove(path)
                self.state.drop(path)
                self._sizes.pop(path, None)
                # don't leave the now-empty Patient/Study/Series folders behind
                prune_empty_dirs(os.path.dirname(path), watch)
                self.log.info(f"Deleted after send: {os.path.basename(path)}", kind="send")
            except OSError as exc:
                self.log.warn(f"Could not delete {path}: {exc}", kind="send")
        elif on_success == "move":
            try:
                rel = os.path.relpath(path, watch)
                target = os.path.join(sent_dir, rel)
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                shutil.move(path, target)
                self.state.drop(path)
                self._sizes.pop(path, None)
                # the file moved to the archive — clear the empty source folders
                # it left in the outgoing tree
                prune_empty_dirs(os.path.dirname(path), watch)
                self.log.info(f"Archived after send: {rel}", kind="send")
            except OSError as exc:
                self.log.warn(f"Could not move {path}: {exc}", kind="send")
        # "keep": leave in place; state already records it as fully sent.

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "sent": self.sent_count,
                "failed": self.failed_count,
                "last_activity": self.last_activity,
            }
