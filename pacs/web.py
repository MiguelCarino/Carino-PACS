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

from flask import Flask, jsonify, request, send_from_directory

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
        edits = {k: d.get(k) for k in ("patient", "patient_id", "study_desc", "series_desc", "study_date") if k in d}
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
