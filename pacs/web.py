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
