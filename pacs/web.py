"""Local web dashboard: a thin REST layer over PacsServer plus the static UI.

Intentionally bound to localhost by default — it exposes start/stop and config
editing with no auth, so it is meant for the machine running the PACS, not the
open network.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from flask import Flask, jsonify, redirect, request, send_file, send_from_directory

from . import APP_NAME, __version__
from .server import PacsServer

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
# When frozen by PyInstaller the package modules live in the archive; the web/
# assets are unpacked under _MEIPASS/pacs/web instead.
if not os.path.isdir(WEB_DIR) and hasattr(sys, "_MEIPASS"):
    WEB_DIR = os.path.join(sys._MEIPASS, "pacs", "web")


def create_app(server: PacsServer) -> Flask:
    app = Flask(__name__, static_folder=None)

    # ---- static UI --------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    # Bundled DICOM-editor (served same-origin so the ✎ Edit deep-link needs no
    # CORS / mixed-content / PNA gymnastics). Sub-assets fall through to the
    # catch-all below; only the trailing-slash index needs its own route so the
    # editor's relative <script> paths resolve under /editor/.
    @app.get("/editor")
    def editor_redirect():
        return redirect("/editor/", code=301)

    @app.get("/editor/")
    def editor_index():
        return send_from_directory(os.path.join(WEB_DIR, "editor"), "index.html")

    @app.get("/<path:filename>")
    def static_files(filename):
        return send_from_directory(WEB_DIR, filename)

    # ---- API --------------------------------------------------------------
    @app.get("/api/status")
    def api_status():
        return jsonify(app=APP_NAME, version=__version__, **server.status())

    @app.get("/api/config")
    def api_get_config():
        return jsonify(server.cfg.data)

    @app.post("/api/config")
    def api_set_config():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="expected a JSON config object"), 400
        try:
            server.apply_config(data)
        except ValueError as exc:          # invalid config
            return jsonify(error=str(exc)), 400
        except OSError as exc:             # e.g. TLS cert/key unreadable, port in use
            return jsonify(error=f"could not apply config: {exc}"), 400
        return jsonify(ok=True, config=server.cfg.data)

    @app.post("/api/receiver")
    def api_receiver():
        action = (request.get_json(silent=True) or {}).get("action")
        try:
            if action == "start":
                server.start_receiver()
            elif action == "stop":
                server.stop_receiver()
            else:
                return jsonify(error="action must be start|stop"), 400
        except OSError as exc:
            return jsonify(error=f"could not start receiver: {exc}"), 400
        return jsonify(ok=True, receiver=server.status()["receiver"])

    @app.post("/api/printer")
    def api_printer():
        action = (request.get_json(silent=True) or {}).get("action")
        try:
            if action == "start":
                server.start_printer()
            elif action == "stop":
                server.stop_printer()
            else:
                return jsonify(error="action must be start|stop"), 400
        except OSError as exc:
            return jsonify(error=f"could not start print receiver: {exc}"), 400
        return jsonify(ok=True, printer=server.status()["printer"])

    @app.post("/api/ris")
    def api_ris():
        action = (request.get_json(silent=True) or {}).get("action")
        try:
            if action == "start":
                server.start_ris()
            elif action == "stop":
                server.stop_ris()
            else:
                return jsonify(error="action must be start|stop"), 400
        except OSError as exc:
            return jsonify(error=f"could not start RIS listener: {exc}"), 400
        return jsonify(ok=True, ris=server.status()["ris"])

    @app.post("/api/emergency")
    def api_emergency():
        action = (request.get_json(silent=True) or {}).get("action")
        res = server.emergency_action(action)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/mwl")
    def api_mwl():
        action = (request.get_json(silent=True) or {}).get("action")
        try:
            if action == "start":
                server.start_mwl()
            elif action == "stop":
                server.stop_mwl()
            else:
                return jsonify(error="action must be start|stop"), 400
        except OSError as exc:
            return jsonify(error=f"could not start worklist SCP: {exc}"), 400
        return jsonify(ok=True, mwl=server.status()["mwl"])

    # ---- RIS orders (emergency RIS: intake + reconciliation) --------------
    @app.get("/api/ris/orders")
    def api_ris_orders():
        status = request.args.get("status") or None
        return jsonify(server.list_orders(status))

    @app.post("/api/ris/orders")
    def api_ris_add_order():
        d = request.get_json(silent=True) or {}
        res = server.add_order(d)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/ris/orders/update")
    def api_ris_update_order():
        d = request.get_json(silent=True) or {}
        oid = d.get("id")
        if not oid:
            return jsonify(ok=False, message="missing 'id'"), 400
        res = server.update_order(oid, d)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/ris/orders/cancel")
    def api_ris_cancel_order():
        oid = (request.get_json(silent=True) or {}).get("id")
        if not oid:
            return jsonify(ok=False, message="missing 'id'"), 400
        res = server.close_order(oid)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/ris/orders/delete")
    def api_ris_delete_order():
        oid = (request.get_json(silent=True) or {}).get("id")
        if not oid:
            return jsonify(ok=False, message="missing 'id'"), 400
        res = server.delete_order(oid)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/ris/orders/purge")
    def api_ris_purge_orders():
        return jsonify(server.purge_closed_orders())

    @app.post("/api/ris/orders/capture")
    def api_ris_capture():
        """Multipart: an order 'id' and a 'file' (PDF/JPEG/PNG) exported from a
        legacy tool, wrapped as a DICOM study inheriting the order's identity."""
        oid = request.form.get("id")
        up = request.files.get("file")
        if not oid or up is None or not up.filename:
            return jsonify(ok=False, message="need an order 'id' and a 'file'"), 400
        res = server.create_study_from_order(oid, up.filename, up.read())
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/watcher")
    def api_watcher():
        action = (request.get_json(silent=True) or {}).get("action")
        if action == "start":
            server.start_watcher()
        elif action == "stop":
            server.stop_watcher()
        else:
            return jsonify(error="action must be start|stop"), 400
        return jsonify(ok=True, watcher=server.status()["watcher"])

    @app.post("/api/echo")
    def api_echo():
        dest = request.get_json(silent=True) or {}
        for k in ("host", "port", "aet"):
            if k not in dest:
                return jsonify(error=f"destination missing '{k}'"), 400
        res = server.echo(dest)
        return jsonify(ok=res.ok, message=res.message)

    @app.get("/api/log")
    def api_log():
        try:
            since = int(request.args.get("since", 0))
        except ValueError:
            since = 0
        return jsonify(last_seq=server.log.last_seq, entries=server.log.since(since))

    # ---- study history (received / sent) ----------------------------------
    @app.get("/api/studies")
    def api_studies():
        group = request.args.get("group", "received")
        try:
            return jsonify(server.list_studies(group))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    def _study_action(fn):
        d = request.get_json(silent=True) or {}
        path = d.get("path")
        if not path:
            return jsonify(ok=False, message="missing 'path'"), 400
        res = fn(d.get("group", "received"), path)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/studies/send")
    def api_studies_send():
        return _study_action(server.send_study)

    @app.post("/api/studies/reveal")
    def api_studies_reveal():
        return _study_action(server.reveal_study)

    @app.post("/api/studies/delete")
    def api_studies_delete():
        return _study_action(server.delete_study)

    @app.post("/api/studies/delete-all")
    def api_studies_delete_all():
        group = (request.get_json(silent=True) or {}).get("group", "received")
        res = server.delete_all_studies(group)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/studies/attach")
    def api_studies_attach():
        """Multipart: 'group', 'path', and a 'file' (PDF/JPEG/PNG) to wrap as a
        DICOM instance attached to that study."""
        group = request.form.get("group", "received")
        path = request.form.get("path")
        up = request.files.get("file")
        if not path or up is None or not up.filename:
            return jsonify(ok=False, message="need a study 'path' and a 'file'"), 400
        res = server.attach_to_study(group, path, up.filename, up.read())
        return jsonify(res), (200 if res.get("ok") else 400)

    # ---- DICOM-editor deep-link (CORS-open, GET-only) ---------------------
    # The editor is a separate origin (a public HTTPS site like
    # dicom.carino.systems, or file://), so these two GET endpoints allow
    # cross-origin reads. When the editor is a PUBLIC page fetching this
    # (private/localhost) PACS, Chrome's Private Network Access sends a CORS
    # preflight expecting `Access-Control-Allow-Private-Network: true`, so we
    # answer OPTIONS and echo that header. Consistent with the dashboard's
    # localhost-only, no-auth posture; nothing here mutates state.
    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        return resp

    def _preflight():
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        resp.headers["Access-Control-Max-Age"] = "600"
        return resp

    @app.route("/api/studies/files", methods=["GET", "OPTIONS"])
    def api_studies_files():
        if request.method == "OPTIONS":
            return _preflight()
        group = request.args.get("group", "received")
        path = request.args.get("path")
        if not path:
            return _cors(jsonify(ok=False, message="missing 'path'")), 400
        res = server.study_dicom_files(group, path)
        return _cors(jsonify(res)), (200 if res.get("ok") else 400)

    @app.route("/api/studies/file", methods=["GET", "OPTIONS"])
    def api_studies_file():
        if request.method == "OPTIONS":
            return _preflight()
        group = request.args.get("group", "received")
        path = request.args.get("path", "")
        name = request.args.get("name", "")
        fp = server.study_dicom_file(group, path, name)
        if not fp:
            return _cors(jsonify(error="not found")), 404
        return _cors(send_file(fp, mimetype="application/dicom",
                               as_attachment=False, download_name=os.path.basename(fp)))

    # ---- stuck sends (failed / backing-off forwards) ----------------------
    @app.get("/api/stuck")
    def api_stuck():
        return jsonify(server.stuck_sends())

    @app.post("/api/stuck/retry")
    def api_stuck_retry():
        dest = (request.get_json(silent=True) or {}).get("dest") or None
        res = server.retry_stuck(dest)
        return jsonify(res), (200 if res.get("ok") else 400)

    # ---- pending imports (non-DICOM awaiting review) ----------------------
    @app.get("/api/pending")
    def api_pending():
        return jsonify(server.list_pending())

    @app.post("/api/pending/approve")
    def api_pending_approve():
        d = request.get_json(silent=True) or {}
        pid = d.get("id")
        if not pid:
            return jsonify(ok=False, message="missing 'id'"), 400
        edits = {k: d.get(k) for k in ("patient", "patient_id", "study_desc", "series_desc", "study_date", "accession") if k in d}
        res = server.approve_pending(pid, edits)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.post("/api/pending/discard")
    def api_pending_discard():
        pid = (request.get_json(silent=True) or {}).get("id")
        if not pid:
            return jsonify(ok=False, message="missing 'id'"), 400
        res = server.discard_pending(pid)
        return jsonify(res), (200 if res.get("ok") else 400)

    @app.get("/api/pending/preview")
    def api_pending_preview():
        pid = request.args.get("id", "")
        loc = server.pending_preview(pid)
        if not loc:
            return jsonify(error="not found"), 404
        folder, filename = loc
        return send_from_directory(folder, filename)

    @app.post("/api/shutdown")
    def api_shutdown():
        """Stop the workers and terminate the whole engine process."""
        server.log.info("Shutdown requested from dashboard", kind="config")
        server.shutdown()

        def _exit():
            time.sleep(0.3)   # let this HTTP response flush first
            os._exit(0)

        threading.Thread(target=_exit, daemon=True).start()
        return jsonify(ok=True, message="Carino PACS is shutting down")

    return app
