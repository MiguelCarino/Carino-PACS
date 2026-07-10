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
from .dicomfs import is_dicom
from .logbuf import LogBuffer
from .scu import Destination, c_store
from .state import SendState


def _dedupe(target: str) -> str:
    """A non-clashing variant of *target* (adds _1, _2… before the extension)."""
    if not os.path.exists(target):
        return target
    base, ext = os.path.splitext(target)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"


def _merge_move(src: str, dst: str) -> None:
    """Recursively move EVERYTHING from *src* into *dst* (files, subfolders,
    non-DICOM and all), then remove the now-empty source tree."""
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            _merge_move(s, d)
        else:
            os.makedirs(os.path.dirname(d) or ".", exist_ok=True)
            shutil.move(s, _dedupe(d))
    os.rmdir(src)


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

    def _candidates(self, watch: str, skip_dirs: list[str]) -> list[str]:
        skips = [os.path.abspath(d) for d in skip_dirs if d]
        found = []
        for root, _dirs, files in os.walk(watch):
            # never rescan the archive or the pending-review store
            aroot = os.path.abspath(root)
            if any(aroot == s or aroot.startswith(s + os.sep) for s in skips):
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
        pending_dir = self.cfg.resolved("scu", "pending_dir")
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

        for path in self._candidates(watch, [sent_dir, pending_dir]):
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

        # Archive/delete whole studies once all their DICOMs are fully sent —
        # moves EVERYTHING (non-DICOM files and subfolders too) and leaves no
        # folders behind in the outgoing tree.
        self._archive_pass(watch, sent_dir, pending_dir, on_success, want)
        self.state.save()

    # --------------------------------------------------------------- archiving
    def _dicoms_under(self, entrypath: str) -> list[str]:
        if os.path.isfile(entrypath):
            return [entrypath] if is_dicom(entrypath) else []
        out = []
        for root, _dirs, files in os.walk(entrypath):
            for f in files:
                if f.startswith("."):
                    continue
                p = os.path.join(root, f)
                if is_dicom(p):
                    out.append(p)
        return out

    def _fully_sent(self, path: str, want: set) -> bool:
        e = self.state.peek(path)
        return bool(e) and want.issubset(set(e.get("sent", [])))

    def _convertibles_under(self, entrypath: str) -> list[tuple[str, str]]:
        """(path, kind) for every non-DICOM PDF/image under *entrypath*."""
        from . import ingest
        if os.path.isfile(entrypath):
            k = None if is_dicom(entrypath) else ingest.detect_kind(entrypath)
            return [(entrypath, k)] if k else []
        out = []
        for root, _dirs, files in os.walk(entrypath):
            for f in files:
                if f.startswith("."):
                    continue
                p = os.path.join(root, f)
                if is_dicom(p):
                    continue
                k = ingest.detect_kind(p)
                if k:
                    out.append((p, k))
        return out

    def _study_identity(self, dicoms: list[str]) -> dict:
        """Patient/study identity read from a sibling DICOM header, so a queued
        report inherits the study it was sitting next to."""
        from .history import _fmt_name, _read_header
        hdr = None
        for p in dicoms:
            hdr = _read_header(p)
            if hdr is not None:
                break
        if hdr is None:
            return {}
        return {
            "patient": _fmt_name(getattr(hdr, "PatientName", "")),
            "patient_name": str(getattr(hdr, "PatientName", "") or ""),
            "patient_id": str(getattr(hdr, "PatientID", "") or ""),
            "study_uid": str(getattr(hdr, "StudyInstanceUID", "") or ""),
            "study_date": str(getattr(hdr, "StudyDate", "") or ""),
            "study_desc": str(getattr(hdr, "StudyDescription", "") or ""),
            "accession": str(getattr(hdr, "AccessionNumber", "") or ""),
        }

    def _siphon_pending(self, entrypath, pending_dir, dicoms, name) -> int:
        """Move any PDF/image beside a study into the pending-review store,
        pre-filling its identity from the study. Returns how many were queued."""
        conv = self._convertibles_under(entrypath)
        if not conv or not pending_dir:
            return 0
        from . import ingest
        identity = self._study_identity(dicoms)
        identity["source"] = name
        queued = 0
        for path, kind in conv:
            try:
                ingest.stage_pending(pending_dir, path, identity, kind)
                queued += 1
            except OSError as exc:
                self.log.warn(f"Could not queue {os.path.basename(path)} for review: {exc}", kind="send")
        return queued

    def _archive_pass(self, watch, sent_dir, pending_dir, on_success, want) -> None:
        """After sending, for each top-level item in the outgoing folder whose
        every DICOM has reached all enabled nodes, move/delete the WHOLE item
        (all files & subfolders) so nothing — empty or not — is left behind."""
        if on_success not in ("move", "delete"):
            return  # "keep": leave everything in place
        try:
            names = os.listdir(watch)
        except OSError:
            return
        sent_abs = os.path.abspath(sent_dir)
        pending_abs = os.path.abspath(pending_dir) if pending_dir else None
        for name in names:
            if name.startswith("."):
                continue
            entrypath = os.path.join(watch, name)
            ap = os.path.abspath(entrypath)
            if ap == sent_abs or ap.startswith(sent_abs + os.sep):
                continue  # never touch the archive itself (if nested under watch)
            if pending_abs and (ap == pending_abs or ap.startswith(pending_abs + os.sep)):
                continue  # never touch the pending store (if nested under watch)
            dicoms = self._dicoms_under(entrypath)
            if not dicoms:
                continue  # not a study (contains no DICOM) — leave it untouched
            if not all(self._fully_sent(f, want) for f in dicoms):
                continue  # some instance still pending/failed — retry next pass
            # Route any PDF/image beside the study to the review queue first, so
            # the archive/delete below only handles DICOM + inert files.
            queued = self._siphon_pending(entrypath, pending_dir, dicoms, name)
            if queued:
                self.log.info(f"Queued {queued} non-DICOM file(s) from {name} for review", kind="send")
            try:
                if on_success == "delete":
                    if os.path.isdir(entrypath):
                        shutil.rmtree(entrypath)
                    else:
                        os.remove(entrypath)
                    verb = "Deleted"
                else:
                    rel = os.path.relpath(entrypath, watch)
                    target = os.path.join(sent_dir, rel)
                    if os.path.isfile(entrypath):
                        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                        shutil.move(entrypath, _dedupe(target))
                    else:
                        _merge_move(entrypath, target)
                    verb = "Archived"
            except OSError as exc:
                self.log.warn(f"Could not {on_success} {name}: {exc}", kind="send")
                continue
            for f in dicoms:
                self.state.drop(f)
                self._sizes.pop(f, None)
            self.log.info(f"{verb} after send: {name}", kind="send")

    def stats(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "sent": self.sent_count,
                "failed": self.failed_count,
                "last_activity": self.last_activity,
            }
