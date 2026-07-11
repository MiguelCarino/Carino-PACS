"""Config load/save + defaults + light validation.

The whole app is configured by one JSON file. By default it lives in
``~/CarinoPACS/config.json`` and the relative folder defaults (``./received``,
``./outgoing``, ``./sent``, ``./logs``) therefore resolve to subfolders of
``~/CarinoPACS`` — so a fresh install keeps everything together in one visible
place in the user's home. Paths are resolved relative to the config file's own
directory (with ``~`` expansion) so they behave predictably no matter the CWD.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

# Default home for config + data + logs: ~/CarinoPACS
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "CarinoPACS")
DEFAULT_CONFIG = os.path.join(DEFAULT_DIR, "config.json")

DEFAULTS: dict[str, Any] = {
    "scp": {
        "aet": "CARINOPACS",
        "bind": "0.0.0.0",
        "port": 11112,
        "storage_dir": "./received",
        "organize": True,
        "allowed_aets": [],
        "tls": False,           # serve DICOM over TLS
        "tls_cert": "",         # server certificate (PEM)
        "tls_key": "",          # server private key (PEM)
        "tls_ca": "",           # if set: require + verify client certs (mutual TLS)
    },
    "scu": {
        "aet": "CARINOSCU",
        "watch_dir": "./outgoing",
        "poll_interval": 3,
        "on_success": "keep",   # keep | move | delete
        "sent_dir": "./sent",
        "pending_dir": "./pending",  # non-DICOM (PDF/image) files awaiting review+convert
        "tls_verify": True,     # verify the remote server's certificate
        "tls_ca": "",           # CA bundle to verify against ("" = system trust store)
        "tls_cert": "",         # our client certificate for mutual TLS (optional)
        "tls_key": "",          # our client private key (optional)
    },
    "destinations": [],
    "web": {
        "host": "127.0.0.1",
        "port": 8042,
        "editor_url": "",       # DICOM-editor base URL; set it to enable the ✎ Edit deep-link
    },
    "logs_dir": "./logs",       # dated log files (one per day) live here
}

_PATH_FIELDS = [("scp", "storage_dir"), ("scu", "watch_dir"), ("scu", "sent_dir"), ("scu", "pending_dir")]


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # ---- persistence -------------------------------------------------------
    def load(self) -> "Config":
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            self.data = _deep_merge(DEFAULTS, raw)
        else:
            self.data = copy.deepcopy(DEFAULTS)
        return self

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)

    def replace(self, new_data: dict) -> "Config":
        """Validate + persist a full config object coming from the dashboard."""
        merged = _deep_merge(DEFAULTS, new_data)
        validate(merged)
        self.data = merged
        self.save()
        return self

    def would_accept(self, new_data: dict) -> None:
        """Validate a candidate config without applying it (raises ValueError)."""
        validate(_deep_merge(DEFAULTS, new_data))

    # ---- convenient views --------------------------------------------------
    @property
    def scp(self) -> dict:
        return self.data["scp"]

    @property
    def scu(self) -> dict:
        return self.data["scu"]

    @property
    def web(self) -> dict:
        return self.data["web"]

    @property
    def destinations(self) -> list[dict]:
        return self.data["destinations"]

    def enabled_destinations(self) -> list[dict]:
        return [d for d in self.destinations if d.get("enabled", True)]

    @property
    def logs_dir(self) -> str:
        """Absolute path of the dated-log folder."""
        return self.resolve_path(self.data.get("logs_dir", "./logs"))

    def resolved(self, section: str, field: str) -> str:
        """Absolute path for a path field, relative to the config file dir."""
        return self.resolve_path(self.data[section][field])

    def resolve_path(self, value: str) -> str:
        """Absolute path for a possibly-relative file path ('~' expanded); '' stays ''."""
        if not value:
            return ""
        value = os.path.expanduser(value)
        if os.path.isabs(value):
            return os.path.normpath(value)
        base = os.path.dirname(self.path)
        return os.path.normpath(os.path.join(base, value))


def validate(data: dict) -> None:
    """Raise ValueError on anything that would make the app misbehave."""
    for section in ("scp", "scu", "web"):
        if not isinstance(data.get(section), dict):
            raise ValueError(f"'{section}' must be an object")

    p = data["scp"]["port"]
    if not (isinstance(p, int) and 1 <= p <= 65535):
        raise ValueError("scp.port must be 1..65535")
    if not str(data["scp"]["aet"]).strip():
        raise ValueError("scp.aet is required")
    if len(str(data["scp"]["aet"])) > 16 or len(str(data["scu"]["aet"])) > 16:
        raise ValueError("AE titles must be 16 characters or fewer")
    if data["scu"]["on_success"] not in ("keep", "move", "delete"):
        raise ValueError("scu.on_success must be keep|move|delete")
    try:
        if float(data["scu"]["poll_interval"]) < 1:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("scu.poll_interval must be a number >= 1")

    if data["scp"].get("tls"):
        if not str(data["scp"].get("tls_cert", "")).strip() or not str(data["scp"].get("tls_key", "")).strip():
            raise ValueError("scp.tls is on but tls_cert / tls_key are not set")

    dests = data.get("destinations", [])
    if not isinstance(dests, list):
        raise ValueError("destinations must be a list")
    for i, d in enumerate(dests):
        for key in ("name", "host", "port", "aet"):
            if key not in d:
                raise ValueError(f"destination #{i + 1} missing '{key}'")
        if not (isinstance(d["port"], int) and 1 <= d["port"] <= 65535):
            raise ValueError(f"destination '{d.get('name')}' has an invalid port")
        if len(str(d["aet"])) > 16:
            raise ValueError(f"destination '{d.get('name')}' AE title too long")
