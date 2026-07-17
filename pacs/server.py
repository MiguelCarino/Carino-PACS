"""Orchestrator — owns the shared Config + LogBuffer and the two workers
(Storage SCP receiver and the folder watcher).  Both the CLI and the web
dashboard drive the app exclusively through this object."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from typing import Optional

from .config import Config
from .emergency import EmergencyController
from .logbuf import LogBuffer
from .mwl import MwlSCP
from .print_scp import PrintSCP
from .ris import OrderStore, RisListener
from .scp import StorageSCP
from .scu import Destination, SendResult, c_echo
from .watcher import FolderWatcher


class PacsServer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.log = LogBuffer(log_dir=cfg.logs_dir)
        self._lock = threading.Lock()
        self.scp: Optional[StorageSCP] = None
        self.print_scp: Optional[PrintSCP] = None
        self.ris: Optional[RisListener] = None
        self.mwl_scp: Optional[MwlSCP] = None
        # The order store is always live (manual entry works even with the HL7
        # listener stopped); the listener is an optional front door onto it.
        self.orders = OrderStore(
            store_dir=cfg.resolved("ris", "store_dir"),
            log=self.log,
            match_on=cfg.ris.get("match_on", "accession"),
        )
        self.watcher = FolderWatcher(cfg, self.log)
        self.emergency = EmergencyController(self, self.log)

    # ---- receiver (Storage SCP) -------------------------------------------
    def start_receiver(self) -> None:
        with self._lock:
            if self.scp and self.scp.running:
                return
            s = self.cfg.scp
            self.scp = StorageSCP(
                aet=s["aet"],
                bind=s.get("bind", "0.0.0.0"),
                port=int(s["port"]),
                storage_dir=self.cfg.resolved("scp", "storage_dir"),
                organize=bool(s.get("organize", True)),
                log=self.log,
                on_received=self._reconcile_study,
                allowed_aets=s.get("allowed_aets", []),
                tls=bool(s.get("tls", False)),
                tls_cert=self.cfg.resolve_path(s.get("tls_cert", "")),
                tls_key=self.cfg.resolve_path(s.get("tls_key", "")),
                tls_ca=self.cfg.resolve_path(s.get("tls_ca", "")),
                min_free_mb=int(float(s.get("min_free_gb", 2) or 0) * 1024),
            )
            self.scp.start()

    def _scu_tls_context(self):
        """Build the client-side TLS context from the current SCU config."""
        from .tlsutil import client_context
        scu = self.cfg.scu
        return client_context(
            verify=bool(scu.get("tls_verify", True)),
            ca=self.cfg.resolve_path(scu.get("tls_ca", "")),
            certfile=self.cfg.resolve_path(scu.get("tls_cert", "")),
            keyfile=self.cfg.resolve_path(scu.get("tls_key", "")),
        )

    def stop_receiver(self) -> None:
        with self._lock:
            if self.scp:
                self.scp.stop()

    def _probe(self, dest: dict):
        """Quiet C-ECHO to a destination for the emergency health monitor —
        returns (ok, message) without logging (it runs every probe interval)."""
        from .scu import Destination, c_echo
        d = Destination.from_dict(dest)
        ctx = None
        if d.tls:
            try:
                ctx = self._scu_tls_context()
            except Exception as exc:
                return False, f"TLS config error: {exc}"
        res = c_echo(d, self.cfg.scu.get("aet", "CARINOSCU"), tls_context=ctx)
        return res.ok, res.message

    # ---- print receiver (virtual DICOM film printer) ----------------------
    def _ingest_print(self, data: bytes, kind: str, identity: dict, name: str) -> None:
        """Sink for a captured print job: stage the rendered film (PDF or image)
        into the pending-review queue (a print carries no trustworthy identity,
        so an operator confirms + approves it before it is ever forwarded)."""
        from . import ingest
        pending_dir = self._pending_dir()
        os.makedirs(pending_dir, exist_ok=True)
        tmp_dir = tempfile.mkdtemp(prefix="carinoprint-")
        tmp = os.path.join(tmp_dir, name)
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            ingest.stage_pending(pending_dir, tmp, identity, kind)
        finally:
            import shutil as _sh
            _sh.rmtree(tmp_dir, ignore_errors=True)

    def start_printer(self) -> None:
        with self._lock:
            if self.print_scp and self.print_scp.running:
                return
            p = self.cfg.printer
            self.print_scp = PrintSCP(
                aet=p.get("aet", "CARINOPRINT"),
                bind=p.get("bind", "0.0.0.0"),
                port=int(p.get("port", 11113)),
                log=self.log,
                on_output=self._ingest_print,
                color=bool(p.get("color", False)),
                layout=p.get("layout", "pdf"),
                allowed_aets=p.get("allowed_aets", []),
                tls=bool(p.get("tls", False)),
                tls_cert=self.cfg.resolve_path(p.get("tls_cert", "")),
                tls_key=self.cfg.resolve_path(p.get("tls_key", "")),
                tls_ca=self.cfg.resolve_path(p.get("tls_ca", "")),
            )
            self.print_scp.start()

    def stop_printer(self) -> None:
        with self._lock:
            if self.print_scp:
                self.print_scp.stop()

    # ---- emergency RIS (HL7/MLLP order intake + reconciliation) -----------
    def start_ris(self) -> None:
        with self._lock:
            if self.ris and self.ris.running:
                return
            r = self.cfg.ris
            # match_on may have changed in config since the store was built.
            self.orders.match_on = r.get("match_on", "accession")
            self.ris = RisListener(
                bind=r.get("bind", "0.0.0.0"),
                port=int(r.get("port", 2575)),
                store=self.orders,
                log=self.log,
                allowed_hosts=r.get("allowed_hosts", []),
            )
            self.ris.start()

    def stop_ris(self) -> None:
        with self._lock:
            if self.ris:
                self.ris.stop()

    # ---- Modality Worklist SCP (serve orders to modalities) ---------------
    def start_mwl(self) -> None:
        with self._lock:
            if self.mwl_scp and self.mwl_scp.running:
                return
            m = self.cfg.mwl
            self.mwl_scp = MwlSCP(
                aet=m.get("aet", "CARINOMWL"),
                bind=m.get("bind", "0.0.0.0"),
                port=int(m.get("port", 11114)),
                log=self.log,
                get_orders=lambda: self.orders.list("open"),
                allowed_aets=m.get("allowed_aets", []),
                tls=bool(m.get("tls", False)),
                tls_cert=self.cfg.resolve_path(m.get("tls_cert", "")),
                tls_key=self.cfg.resolve_path(m.get("tls_key", "")),
                tls_ca=self.cfg.resolve_path(m.get("tls_ca", "")),
            )
            self.mwl_scp.start()

    def stop_mwl(self) -> None:
        with self._lock:
            if self.mwl_scp:
                self.mwl_scp.stop()

    def worklist_wanted(self) -> bool:
        """True if the Modality Worklist should run as a permanent service: the
        SCP is explicitly enabled, OR any enabled destination is flagged
        ``no_ris`` (that PACS has no RIS, so Carino is its worklist source)."""
        if self.cfg.mwl.get("enabled"):
            return True
        return any(d.get("no_ris") for d in self.cfg.enabled_destinations())

    def sync_worklist(self) -> None:
        """Start the worklist SCP if it's wanted and not already running
        (called on launch and after a config change)."""
        if self.worklist_wanted() and not (self.mwl_scp and self.mwl_scp.running):
            try:
                self.start_mwl()
            except Exception as exc:
                self.log.error(f"Could not start worklist SCP: {exc}", kind="mwl")

    def _reconcile_study(self, ds, path: str) -> None:
        """Called for every C-STORE'd instance: try to match it to an open RIS
        order by Accession Number (or Patient ID fallback). On a hit, close +
        archive the order. Delivery of the study is NEVER gated on this — the
        instance is already stored; this only reconciles order tracking."""
        accession = str(getattr(ds, "AccessionNumber", "") or "")
        patient_id = str(getattr(ds, "PatientID", "") or "")
        study_uid = str(getattr(ds, "StudyInstanceUID", "") or "")
        # Hold-and-forward: while emergency failover is active, copy every
        # received instance into the outgoing folder so the watcher forwards it
        # to the primary (retrying/holding until it's back). Independent of
        # whether the study matches an order.
        if self.emergency.active and self.cfg.emergency.get("hold_and_forward", True):
            self._queue_for_forward(path)
        if not accession and not patient_id and not study_uid:
            return
        # Study Instance UID is the strongest key (exact when the exam was made
        # from a Carino order via MWL); accession / patient id are fallbacks.
        order = self.orders.match(accession, patient_id, study_uid)
        if not order:
            return
        if self.cfg.ris.get("auto_close", True):
            self.orders.close(order["id"], reason="matched", matched_study=study_uid)
            self.log.info(
                f"RIS order matched + closed: {order.get('patient') or '?'} "
                f"[acc {order.get('accession') or '—'}] ← study {os.path.basename(path)}",
                kind="ris",
            )
        else:
            self.log.info(
                f"RIS order matched (left open — auto-close off): "
                f"{order.get('patient') or '?'} [acc {order.get('accession') or '—'}]",
                kind="ris",
            )

    def _queue_for_forward(self, path: str) -> None:
        """Copy a received instance into the outgoing watch folder so the normal
        auto-send/retry pipeline forwards it to the primary (used by emergency
        hold-and-forward). Best-effort — never break the C-STORE on a copy error."""
        import shutil as _sh
        try:
            watch = self.cfg.resolved("scu", "watch_dir")
            os.makedirs(watch, exist_ok=True)
            dst = os.path.join(watch, os.path.basename(path))
            if os.path.abspath(dst) != os.path.abspath(path) and not os.path.exists(dst):
                _sh.copy2(path, dst)
        except OSError as exc:
            self.log.warn(f"Emergency hold-and-forward: could not queue {os.path.basename(path)}: {exc}",
                          kind="emergency")

    # ---- emergency failover (health monitor + state machine) --------------
    def emergency_action(self, action: str) -> dict:
        """Drive the failover state machine from the dashboard."""
        fn = {
            "arm": self.emergency.arm,
            "disarm": self.emergency.disarm,
            "activate": self.emergency.activate,
            "dismiss": self.emergency.dismiss,
            "resume": self.emergency.resume,
        }.get(action)
        if not fn:
            return {"ok": False, "message": "action must be arm|disarm|activate|dismiss|resume"}
        return {"ok": True, "emergency": fn()}

    # ---- RIS orders (CRUD over the store) ---------------------------------
    def list_orders(self, status: Optional[str] = None) -> dict:
        return {"orders": self.orders.list(status), "counts": self.orders.counts()}

    def add_order(self, fields: dict) -> dict:
        if not any(str(fields.get(k, "")).strip() for k in ("accession", "patient", "patient_id")):
            return {"ok": False, "message": "an order needs at least an accession, patient name or patient ID"}
        order = self.orders.add(fields, source="manual")
        return {"ok": True, "message": "Order queued", "order": order}

    def update_order(self, oid: str, fields: dict) -> dict:
        o = self.orders.update(oid, fields)
        if not o:
            return {"ok": False, "message": "order not found"}
        self.log.info(f"RIS order edited [acc {o.get('accession') or '—'}]", kind="ris")
        return {"ok": True, "message": "Order updated", "order": o}

    def close_order(self, oid: str) -> dict:
        o = self.orders.close(oid, reason="cancelled")
        if not o:
            return {"ok": False, "message": "order not found"}
        self.log.info(f"RIS order cancelled [acc {o.get('accession') or '—'}]", kind="ris")
        return {"ok": True, "message": "Order cancelled"}

    def delete_order(self, oid: str) -> dict:
        ok = self.orders.delete(oid)
        return {"ok": ok, "message": "Order deleted" if ok else "order not found"}

    def purge_closed_orders(self) -> dict:
        n = self.orders.purge_closed()
        self.log.info(f"Purged {n} closed RIS order(s)", kind="ris")
        return {"ok": True, "removed": n, "message": f"Removed {n} closed order(s)"}

    def create_study_from_order(self, order_id: str, filename: str, data: bytes) -> dict:
        """Use-case-B bridge: wrap an exported PDF/image as a DICOM study that
        inherits THIS order's identity (patient, IDs, accession, and the order's
        pre-generated Study Instance UID), drop it into the outgoing folder for
        the normal auto-send/hold-and-forward pipeline, and close the order as
        fulfilled. The tech captured the study in a legacy tool and relates the
        export to the on-screen order — no hand-typed identity."""
        from . import ingest
        order = self.orders.get(order_id)
        if not order:
            return {"ok": False, "message": "order not found"}
        if order.get("status") != "open":
            return {"ok": False, "message": "order is already closed"}
        kind = ingest.detect_kind_bytes(data, filename)
        if not kind:
            return {"ok": False, "message": "unsupported file — capture a PDF, JPEG or PNG"}
        base = os.path.splitext(os.path.basename(filename))[0]
        meta = {
            "patient": order.get("patient", ""),
            "patient_name": order.get("patient_name", ""),
            "patient_id": order.get("patient_id", ""),
            "patient_birthdate": order.get("patient_birthdate", ""),
            "patient_sex": order.get("patient_sex", ""),
            "study_uid": order.get("study_uid", ""),
            "study_date": order.get("scheduled_dt", ""),
            "study_desc": order.get("study_desc", ""),
            "accession": order.get("accession", ""),
            "referring": order.get("referring", ""),
            "series_desc": base or order.get("study_desc") or "Captured study",
            "source": "RIS order " + (order.get("accession") or order_id),
        }
        watch = self.cfg.resolved("scu", "watch_dir")
        try:
            ds = ingest.build_from_bytes(data, kind, meta)
            out = ingest.save_instance(ds, watch)
        except Exception as exc:
            return {"ok": False, "message": f"could not convert: {exc}"}
        self.orders.close(order_id, reason="captured", matched_study=order.get("study_uid", ""))
        self.log.info(
            f"Captured study for order [acc {order.get('accession') or '—'}] "
            f"→ {os.path.basename(out)} into outgoing; order closed",
            kind="ris",
        )
        if self.watcher.running:
            msg = "Study created and queued — Auto-send will forward it (held until the PACS is reachable)."
        else:
            msg = "Study created in the outgoing folder — start Auto-send to forward it."
        return {"ok": True, "message": msg, "file": os.path.basename(out)}

    # ---- watcher (auto-send) ----------------------------------------------
    def start_watcher(self) -> None:
        self.watcher.start()

    def stop_watcher(self) -> None:
        self.watcher.stop()

    # ---- one-off actions ---------------------------------------------------
    def echo(self, dest: dict) -> SendResult:
        d = Destination.from_dict(dest)
        self.log.info(f"C-ECHO -> {d.name} ({d.host}:{d.port}){' [TLS]' if d.tls else ''}", kind="echo")
        ctx = None
        if d.tls:
            try:
                ctx = self._scu_tls_context()
            except Exception as exc:  # bad cert/key/CA path
                self.log.warn(f"C-ECHO {d.name}: TLS config error: {exc}", kind="echo")
                return SendResult(False, f"TLS config error: {exc}")
        res = c_echo(d, self.cfg.scu.get("aet", "CARINOSCU"), tls_context=ctx)
        (self.log.info if res.ok else self.log.warn)(
            f"C-ECHO {d.name}: {res.message}", kind="echo"
        )
        return res

    # ---- study history / browse -------------------------------------------
    def _group_root(self, group: str) -> Optional[str]:
        """Resolve a history 'group' to its storage folder."""
        if group == "received":
            return self.cfg.resolved("scp", "storage_dir")
        if group in ("sent", "archived"):
            return self.cfg.resolved("scu", "sent_dir")
        if group == "outgoing":
            return self.cfg.resolved("scu", "watch_dir")
        return None

    def list_studies(self, group: str) -> dict:
        from . import history
        root = self._group_root(group)
        if root is None:
            raise ValueError("group must be received|sent")
        return {"group": group, "root": root, "studies": history.scan_studies(root)}

    def delete_study(self, group: str, path: str) -> dict:
        from . import history
        root = self._group_root(group)
        if root is None:
            return {"ok": False, "message": "group must be received|sent"}
        try:
            history.delete_study(root, path)
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        self.log.info(f"Deleted study {os.path.basename(path)} from {group}", kind="config")
        return {"ok": True, "message": "Study deleted"}

    def delete_all_studies(self, group: str) -> dict:
        from . import history
        root = self._group_root(group)
        if root is None:
            return {"ok": False, "message": "group must be received|sent"}
        n = history.delete_all(root)
        self.log.info(f"Deleted all {group} studies ({n} removed)", kind="config")
        return {"ok": True, "removed": n, "message": f"Removed {n} studies"}

    def reveal_study(self, group: str, path: str) -> dict:
        root = self._group_root(group)
        from .dicomfs import safe_within
        if root is None or not safe_within(root, path):
            return {"ok": False, "message": "path is outside the storage folder"}
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        if not os.path.exists(folder):
            return {"ok": False, "message": "folder no longer exists"}
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)   # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            return {"ok": False, "message": f"could not open folder: {exc}"}
        return {"ok": True, "message": f"Opened {folder}"}

    def send_study(self, group: str, path: str) -> dict:
        """Forward every instance of a study to all enabled destinations.

        Runs in a background thread so a big study doesn't block the request;
        per-file results stream to the Activity log (kind='send')."""
        from . import history
        from .scu import Destination, c_store
        root = self._group_root(group)
        if root is None:
            return {"ok": False, "message": "group must be received|sent"}
        try:
            files = history.study_files(root, path)
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        if not files:
            return {"ok": False, "message": "no DICOM files found for this study"}
        dests = [Destination.from_dict(d) for d in self.cfg.enabled_destinations()]
        if not dests:
            return {"ok": False, "message": "no enabled destinations — add one in Destinations first"}
        ctx = None
        if any(d.tls for d in dests):
            try:
                ctx = self._scu_tls_context()
            except Exception as exc:
                return {"ok": False, "message": f"TLS config error: {exc}"}
        aet = self.cfg.scu.get("aet", "CARINOSCU")
        label = os.path.basename(path.rstrip("/\\")) or "study"

        def _run():
            ok = fail = 0
            for fp in files:
                for d in dests:
                    res = c_store(d, fp, aet, tls_context=ctx)
                    if res.ok:
                        ok += 1
                        with self.watcher._lock:
                            self.watcher.sent_count += 1
                            self.watcher.last_activity = f"{os.path.basename(fp)} -> {d.name}"
                        self.log.info(f"Sent {os.path.basename(fp)} -> {d.name}", kind="send")
                    else:
                        fail += 1
                        with self.watcher._lock:
                            self.watcher.failed_count += 1
                        self.log.warn(f"Send {os.path.basename(fp)} -> {d.name}: {res.message}", kind="send")
            self.log.info(
                f"Manual send of {label} finished: {ok} ok, {fail} failed "
                f"({len(files)} instance(s) → {len(dests)} node(s))",
                kind="send",
            )

        threading.Thread(target=_run, name="pacs-send", daemon=True).start()
        return {"ok": True, "message": f"Sending {len(files)} instance(s) to {len(dests)} destination(s)…"}

    def attach_to_study(self, group: str, path: str, filename: str, data: bytes) -> dict:
        """Wrap an uploaded PDF/image as a DICOM instance inheriting the target
        study's identity and drop it into the study's folder as a new series.
        The user then hits Send/Resend to forward the study (report included)."""
        from . import history, ingest
        from .dicomfs import safe_within
        root = self._group_root(group)
        if root is None:
            return {"ok": False, "message": "group must be received|sent"}
        if not safe_within(root, path):
            return {"ok": False, "message": "path is outside the storage folder"}
        kind = ingest.detect_kind_bytes(data, filename)
        if not kind:
            return {"ok": False, "message": "unsupported file — attach a PDF, JPEG or PNG"}
        try:
            identity = history.study_identity(root, path)
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        if not identity:
            return {"ok": False, "message": "could not read the study's patient/identity"}
        identity["series_desc"] = os.path.splitext(os.path.basename(filename))[0] or "Attachment"
        study_dir = path if os.path.isdir(path) else os.path.dirname(path)
        # Land it in its own subfolder so it reads as a separate DOC/OT series
        # (the browser groups a study one modality per folder).
        dest_dir = os.path.join(study_dir, "attachments")
        try:
            ds = ingest.build_from_bytes(data, kind, identity)
            out = ingest.save_instance(ds, dest_dir)
        except Exception as exc:
            return {"ok": False, "message": f"could not convert: {exc}"}
        self.log.info(f"Attached {filename} to study {os.path.basename(study_dir)} ({group})", kind="config")
        return {"ok": True, "message": f"Attached {filename} — hit {'Resend' if group in ('sent', 'archived') else 'Send'} to forward it",
                "file": os.path.basename(out)}

    # ---- DICOM-editor deep-link -------------------------------------------
    def study_dicom_files(self, group: str, path: str) -> dict:
        """Manifest of a study's DICOM files ({name, url}) for the DICOM-editor
        deep-link to fetch. Reuses study_files' root gate."""
        from . import history
        from urllib.parse import urlencode
        root = self._group_root(group)
        if root is None:
            return {"ok": False, "message": "group must be received|sent"}
        try:
            files = history.study_files(root, path)
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        if not files:
            return {"ok": False, "message": "no DICOM files found for this study"}
        base = path if os.path.isdir(path) else os.path.dirname(path)
        out = []
        for fp in files:
            name = os.path.relpath(fp, base)
            url = "/api/studies/file?" + urlencode({"group": group, "path": path, "name": name})
            out.append({"name": name, "url": url})
        return {"ok": True, "files": out}

    def study_dicom_file(self, group: str, path: str, name: str) -> Optional[str]:
        """Absolute path of one named DICOM file in a study, or None. Only files
        that study_files already vouched for (in-root, is_dicom) can match, so a
        crafted 'name' can't escape the study."""
        from . import history
        root = self._group_root(group)
        if root is None:
            return None
        try:
            files = history.study_files(root, path)
        except (ValueError, OSError):
            return None
        base = path if os.path.isdir(path) else os.path.dirname(path)
        for fp in files:
            if os.path.relpath(fp, base) == name:
                return fp
        return None

    # ---- pending imports (non-DICOM awaiting review) ----------------------
    def _pending_dir(self) -> str:
        return self.cfg.resolved("scu", "pending_dir")

    def list_pending(self) -> dict:
        from . import ingest
        d = self._pending_dir()
        return {"root": d, "items": ingest.list_pending(d)}

    def approve_pending(self, pid: str, edits: dict) -> dict:
        """Convert a queued file into the outgoing folder so the normal
        auto-send + archive pipeline forwards and files it."""
        from . import ingest
        watch = self.cfg.resolved("scu", "watch_dir")
        try:
            out = ingest.approve_pending(self._pending_dir(), pid, edits or {}, watch)
        except (ValueError, OSError) as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": f"could not convert: {exc}"}
        self.log.info(f"Approved review item → {os.path.basename(out)} into outgoing", kind="config")
        if self.watcher.running:
            msg = "Converted and queued — Auto-send will forward it."
        else:
            msg = "Converted into the outgoing folder — start Auto-send to forward it."
        return {"ok": True, "message": msg}

    def discard_pending(self, pid: str) -> dict:
        from . import ingest
        try:
            ok = ingest.discard_pending(self._pending_dir(), pid)
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        return {"ok": ok, "message": "Discarded" if ok else "item not found"}

    def pending_preview(self, pid: str):
        """(folder, filename) of a queued file's raw bytes, or None."""
        from . import ingest
        try:
            return ingest.preview_path(self._pending_dir(), pid)
        except ValueError:
            return None

    # ---- config ------------------------------------------------------------
    def apply_config(self, new_data: dict) -> None:
        """Persist a new config from the dashboard and hot-apply it.

        The receiver is bound to a port/AE at start time, so if it is running
        we bounce it; the watcher reads config live, so it just keeps going.
        """
        # Validate the candidate first so a bad post never disturbs a running
        # receiver (raises ValueError, surfaced to the caller as a 400).
        self.cfg.would_accept(new_data)
        was_receiving = bool(self.scp and self.scp.running)
        was_printing = bool(self.print_scp and self.print_scp.running)
        was_ris = bool(self.ris and self.ris.running)
        was_mwl = bool(self.mwl_scp and self.mwl_scp.running)
        self.stop_receiver()
        self.stop_printer()
        self.stop_ris()
        self.stop_mwl()
        self.cfg.replace(new_data)
        self.log.log_dir = self.cfg.logs_dir   # logs_dir may have changed
        # store_dir / match_on may have changed — repoint the live order store.
        self.orders.store_dir = self.cfg.resolved("ris", "store_dir")
        self.orders.match_on = self.cfg.ris.get("match_on", "accession")
        if was_receiving:
            self.start_receiver()
        if was_printing:
            self.start_printer()
        if was_ris:
            self.start_ris()
        if was_mwl:
            self.start_mwl()
        self.sync_worklist()   # a no_ris destination may now want a permanent worklist
        # Re-sync the health monitor to the new config (armed flag / trigger set).
        self.emergency.stop()
        self.emergency.start()
        self.log.info("Configuration updated", kind="config")

    # ---- status ------------------------------------------------------------
    @staticmethod
    def _local_ip() -> Optional[str]:
        """The machine's primary LAN IP (the address remote nodes would use to
        reach this receiver), or None when there is no network route."""
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.2)
            s.connect(("8.8.8.8", 80))     # no packets sent; just resolves the source IP
            ip = s.getsockname()[0]
            return ip if ip and not ip.startswith("127.") else None
        except OSError:
            return None
        finally:
            s.close()

    @staticmethod
    def _local_ips() -> list:
        """Every non-loopback IPv4 address on this host, so an operator can point
        a modality on ANY local subnet at the right one. Default-route IP first,
        the rest sorted. Handles a multi-homed host with several device networks
        (and an air-gapped device subnet that has no default route at all)."""
        import socket
        found: list = []
        try:
            import psutil
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if (a.family == socket.AF_INET and a.address
                            and not a.address.startswith("127.")
                            and a.address not in found):
                        found.append(a.address)
        except Exception:                       # psutil missing / platform quirk
            pass
        primary = PacsServer._local_ip()        # default-route source IP (or None)
        if primary and primary in found:
            found.remove(primary)
        found.sort()
        if primary:
            found.insert(0, primary)
        return found

    # ---- stuck sends (failed / backing-off forwards) ----------------------
    def _enabled_dest_names(self) -> set:
        return {d.get("name", "") for d in self.cfg.enabled_destinations()}

    def stuck_sends(self) -> dict:
        """Studies whose forward to an enabled destination has FAILED at least
        once and is still outstanding, grouped per destination. Freshly-queued
        (never-attempted) files are not 'stuck' — only ones with a recorded
        failure. So an operator can see a node that's down and why."""
        import time
        want = self._enabled_dest_names()
        per: dict = {}
        files = 0
        now = time.time()
        for path, e in self.watcher.state.all_entries().items():
            if not os.path.exists(path):
                continue
            sent = set(e.get("sent", []))
            fails = e.get("fail", {}) or {}
            stuck_here = False
            for dname in want:
                if dname in sent:
                    continue
                f = fails.get(dname)
                if not f:
                    continue                       # queued but not yet failed
                stuck_here = True
                agg = per.setdefault(dname, {"name": dname, "instances": 0,
                                             "attempts": 0, "last_error": "", "next_try": float("inf")})
                agg["instances"] += 1
                agg["attempts"] = max(agg["attempts"], int(f.get("attempts", 0)))
                agg["last_error"] = f.get("last_error", "") or agg["last_error"]
                agg["next_try"] = min(agg["next_try"], float(f.get("next_try", 0) or 0))
            if stuck_here:
                files += 1
        dests = sorted(per.values(), key=lambda x: -x["instances"])
        for d in dests:
            d["next_in"] = max(0, int(d.pop("next_try") - now))
        return {"destinations": dests, "files": files}

    def stuck_count(self) -> int:
        return self.stuck_sends()["files"]

    def retry_stuck(self, dest: Optional[str] = None) -> dict:
        """Clear the retry backoff so the next watcher pass attempts immediately
        (all stuck destinations, or just `dest`)."""
        names = {dest} if dest else None
        n = self.watcher.state.clear_backoff(names)
        self.watcher.state.save()
        if not self.watcher.running:
            return {"ok": True, "reset": n,
                    "message": f"Cleared backoff on {n} item(s) — start Auto-send to retry them."}
        return {"ok": True, "reset": n, "message": f"Retrying {n} item(s) now…"}

    # ---- disk headroom on the storage volume ------------------------------
    def _disk_status(self) -> dict:
        import shutil as _sh
        path = self.cfg.resolved("scp", "storage_dir")
        probe = path if os.path.isdir(path) else (os.path.dirname(path) or ".")
        floor_gb = float(self.cfg.scp.get("min_free_gb", 2) or 0)
        try:
            u = _sh.disk_usage(probe)
            free_gb = u.free / (1024 ** 3)
            return {
                "path": path,
                "free_gb": round(free_gb, 1),
                "total_gb": round(u.total / (1024 ** 3), 1),
                "free_pct": round(100 * u.free / u.total, 1) if u.total else 0,
                "floor_gb": floor_gb,
                "low": bool(floor_gb > 0 and free_gb < floor_gb),
            }
        except OSError:
            return {"path": path, "free_gb": None, "low": False, "floor_gb": floor_gb}

    def status(self) -> dict:
        from . import ingest
        scp = self.scp
        pscp = self.print_scp
        pr = self.cfg.printer
        ris = self.ris
        rcfg = self.cfg.ris
        mwl = self.mwl_scp
        mcfg = self.cfg.mwl
        return {
            "receiver": {
                "running": bool(scp and scp.running),
                "aet": self.cfg.scp["aet"],
                "bind": self.cfg.scp.get("bind", "0.0.0.0"),
                "port": self.cfg.scp["port"],
                "storage_dir": self.cfg.resolved("scp", "storage_dir"),
                "organize": self.cfg.scp.get("organize", True),
                "received": scp.received_count if scp else 0,
                "errors": scp.error_count if scp else 0,
                "refused": scp.refused_count if scp else 0,
                "tls": bool(self.cfg.scp.get("tls", False)),
                "tls_mutual": bool(self.cfg.scp.get("tls", False) and self.cfg.scp.get("tls_ca", "")),
            },
            "printer": {
                "enabled": bool(pr.get("enabled", False)),
                "running": bool(pscp and pscp.running),
                "aet": pr.get("aet", "CARINOPRINT"),
                "bind": pr.get("bind", "0.0.0.0"),
                "port": int(pr.get("port", 11113)),
                "color": bool(pr.get("color", False)),
                "layout": pr.get("layout", "pdf"),
                "printed": pscp.printed_count if pscp else 0,
                "errors": pscp.error_count if pscp else 0,
                "tls": bool(pr.get("tls", False)),
            },
            "watcher": {
                **self.watcher.stats(),
                "watch_dir": self.cfg.resolved("scu", "watch_dir"),
                "aet": self.cfg.scu.get("aet", "CARINOSCU"),
                "on_success": self.cfg.scu.get("on_success", "keep"),
                "poll_interval": self.cfg.scu.get("poll_interval", 3),
                "tls_verify": bool(self.cfg.scu.get("tls_verify", True)),
            },
            "ris": {
                "enabled": bool(rcfg.get("enabled", False)),
                "running": bool(ris and ris.running),
                "bind": rcfg.get("bind", "0.0.0.0"),
                "port": int(rcfg.get("port", 2575)),
                "match_on": rcfg.get("match_on", "accession"),
                "auto_close": bool(rcfg.get("auto_close", True)),
                "received": ris.received_count if ris else 0,
                "orders_in": ris.order_count if ris else 0,
                "errors": ris.error_count if ris else 0,
                "counts": self.orders.counts(),
            },
            "mwl": {
                "enabled": bool(mcfg.get("enabled", False)),
                "running": bool(mwl and mwl.running),
                "aet": mcfg.get("aet", "CARINOMWL"),
                "bind": mcfg.get("bind", "0.0.0.0"),
                "port": int(mcfg.get("port", 11114)),
                "queries": mwl.query_count if mwl else 0,
                "matches": mwl.match_count if mwl else 0,
                "errors": mwl.error_count if mwl else 0,
                "tls": bool(mcfg.get("tls", False)),
                "wanted": self.worklist_wanted(),   # permanent (enabled or a no_ris destination)
            },
            "emergency": self.emergency.status(),
            "destinations": self.cfg.destinations,
            "config_path": self.cfg.path,
            "logs_dir": self.cfg.logs_dir,
            "host_ip": self._local_ip(),
            "host_ips": self._local_ips(),
            "pending": ingest.count_pending(self._pending_dir()),
            "stuck": self.stuck_count(),
            "disk": self._disk_status(),
            "editor_url": self.cfg.web.get("editor_url", ""),
        }

    def shutdown(self) -> None:
        self.emergency.stop()
        self.stop_watcher()
        self.stop_receiver()
        self.stop_printer()
        self.stop_ris()
        self.stop_mwl()
