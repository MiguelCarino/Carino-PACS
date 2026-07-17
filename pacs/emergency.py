"""Emergency failover — health monitor + state machine.

Watches the destinations flagged ``emergency_trigger`` (the primary PACS). When
one is unreachable beyond a threshold, it raises an **emergency**: by default it
does not silently open sockets — it flips a ``prompt`` flag the dashboard turns
into a "primary PACS unreachable — activate emergency RIS?" pop-up. The operator
**activates** (start the worklist SCP + hold-and-forward) or **dismisses** it.

State machine (see docs/ris-emergency-design.md):

    off ──arm()──▶ idle ──trigger detected──▶ triggered ──activate()──▶ active
     ▲              ▲   ◀── recovered ───────────┘                        │
     └──disarm()────┘                                          primary verified back
                    ▲                                                     │
                    └──────────── resume() ◀── recovering ◀──────────────┘

Detection uses **both** signals (locked decision): an active periodic C-ECHO
probe *and* the watcher's passive send-failures. Recovery needs
``recovery_successes`` consecutive good probes (hysteresis) so a flapping link
can't rattle the state. On recovery the held studies are auto-flushed, but the
operator must click **Resume normal** to fully stand down (no auto-exit).

The controller is deliberately thin: it drives the state and calls back into the
``PacsServer`` for the DICOM-facing actions (probe, start/stop worklist, flush).
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .logbuf import LogBuffer

# State constants.
OFF = "off"                # not armed
IDLE = "idle"              # armed, everything healthy
TRIGGERED = "triggered"    # a primary is offline, awaiting operator decision
ACTIVE = "active"          # emergency running (worklist + hold-and-forward)
RECOVERING = "recovering"  # primary back, held studies flushing, awaiting Resume


class _Health:
    """Per-destination reachability tracker."""
    __slots__ = ("online", "consecutive_fails", "consecutive_ok",
                 "offline_since", "last_error", "last_probe")

    def __init__(self):
        self.online = True
        self.consecutive_fails = 0
        self.consecutive_ok = 0
        self.offline_since: Optional[float] = None
        self.last_error = ""
        self.last_probe: Optional[float] = None


class EmergencyController:
    def __init__(self, server, log: LogBuffer):
        self.server = server
        self.log = log
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._health: dict[str, _Health] = {}
        self.state = OFF
        self.trigger_dest = ""
        self.since = 0.0
        self.prompt_dismissed = False
        self._now = time.time

    # ---- config views ------------------------------------------------------
    @property
    def _cfg(self) -> dict:
        return self.server.cfg.emergency

    @property
    def armed(self) -> bool:
        return bool(self._cfg.get("armed", False))

    @property
    def active(self) -> bool:
        return self.state in (ACTIVE, RECOVERING)

    def _trigger_dests(self) -> list:
        return [d for d in self.server.cfg.enabled_destinations() if d.get("emergency_trigger")]

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Start the monitor thread if failover is armed (idempotent)."""
        if not self.armed or (self._thread and self._thread.is_alive()):
            if self.armed and self.state == OFF:
                self.state = IDLE
            return
        self._stop.clear()
        self.state = IDLE
        self._thread = threading.Thread(target=self._loop, name="pacs-emergency", daemon=True)
        self._thread.start()
        n = len(self._trigger_dests())
        self.log.info(
            f"Emergency failover armed — monitoring {n} destination(s) "
            f"(probe every {self._cfg.get('probe_interval_sec', 30)}s)",
            kind="emergency",
        )

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.state = OFF

    def arm(self) -> dict:
        self._cfg["armed"] = True
        self.server.cfg.save()
        self.start()
        return self.status()

    def disarm(self) -> dict:
        self._cfg["armed"] = False
        self.server.cfg.save()
        self.stop()
        with self._lock:
            self._health.clear()
            self.trigger_dest = ""
            self.prompt_dismissed = False
        self.log.info("Emergency failover disarmed", kind="emergency")
        return self.status()

    # ---- operator actions --------------------------------------------------
    def activate(self) -> dict:
        """Operator confirmed the pop-up: bring up the local emergency services.
        Starts the Modality Worklist SCP and turns on hold-and-forward so studies
        received during the outage queue for the primary and back-fill on return."""
        with self._lock:
            self.state = ACTIVE
            self.since = self._now()
            self.prompt_dismissed = False
        try:
            self.server.start_mwl()
        except Exception as exc:
            self.log.error(f"Emergency: worklist SCP failed to start: {exc}", kind="emergency")
        # A running watcher is what forwards (and holds+retries) the studies.
        try:
            if not self.server.watcher.running:
                self.server.start_watcher()
        except Exception as exc:
            self.log.error(f"Emergency: auto-send failed to start: {exc}", kind="emergency")
        self.log.warn(
            f"EMERGENCY ACTIVATED — primary '{self.trigger_dest}' unreachable. "
            f"Worklist serving; received studies held for forward.",
            kind="emergency",
        )
        return self.status()

    def dismiss(self) -> dict:
        """Operator dismissed the pop-up: keep monitoring + banner, but don't
        start emergency services or re-prompt for this outage."""
        with self._lock:
            self.prompt_dismissed = True
        self.log.info("Emergency prompt dismissed — failover not activated", kind="emergency")
        return self.status()

    def resume(self) -> dict:
        """Operator stood down: stop the worklist SCP and return to armed/idle."""
        try:
            self.server.stop_mwl()
        except Exception:
            pass
        with self._lock:
            self.state = IDLE if self.armed else OFF
            self.trigger_dest = ""
            self.prompt_dismissed = False
        self.log.info("Emergency resolved — resumed normal operation", kind="emergency")
        return self.status()

    # ---- monitor loop ------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # never let the monitor die
                self.log.error(f"Emergency monitor error: {exc}", kind="emergency")
            self._wake.clear()
            self._wake.wait(max(5, int(self._cfg.get("probe_interval_sec", 30))))

    def _tick(self) -> None:
        dests = self._trigger_dests()
        threshold = float(self._cfg.get("offline_threshold_sec", 120))
        need_ok = int(self._cfg.get("recovery_successes", 2))
        now = self._now()

        try:
            stuck = {d["name"] for d in self.server.stuck_sends().get("destinations", [])}
        except Exception:
            stuck = set()

        offline_names = []
        for d in dests:
            name = d.get("name", "")
            h = self._health.setdefault(name, _Health())
            ok, msg = self.server._probe(d)
            failing = (not ok) or (name in stuck)     # both signals
            h.last_probe = now
            if failing:
                h.consecutive_fails += 1
                h.consecutive_ok = 0
                h.last_error = msg if not ok else "forward failing"
                if h.offline_since is None:
                    h.offline_since = now
                if h.online and (now - h.offline_since) >= threshold:
                    h.online = False
                    self.log.warn(f"Emergency: '{name}' is OFFLINE ({h.last_error})", kind="emergency")
            else:
                h.consecutive_ok += 1
                h.consecutive_fails = 0
                h.last_error = ""
                if not h.online and h.consecutive_ok >= need_ok:
                    h.online = True
                    h.offline_since = None
                    self.log.info(f"Emergency: '{name}' is back ONLINE", kind="emergency")
                elif h.online:
                    h.offline_since = None
            if not h.online:
                offline_names.append(name)

        self._evaluate(offline_names)

    def _evaluate(self, offline_names: list) -> None:
        with self._lock:
            if not self.armed:
                self.state = OFF
                return
            any_offline = bool(offline_names)

            if self.state == IDLE and any_offline:
                self.trigger_dest = offline_names[0]
                self.since = self._now()
                self.prompt_dismissed = False
                if self._cfg.get("auto_activate"):
                    self._unlocked_activate = True   # handled below outside the lock
                else:
                    self.state = TRIGGERED
                    self.log.warn(
                        f"Emergency TRIGGERED — '{self.trigger_dest}' unreachable; "
                        f"awaiting operator decision", kind="emergency")

            elif self.state == TRIGGERED and not any_offline:
                self.state = IDLE
                self.trigger_dest = ""
                self.prompt_dismissed = False
                self.log.info("Emergency stand-down — primary recovered before activation",
                              kind="emergency")

            elif self.state == ACTIVE and not any_offline:
                self.state = RECOVERING

        # auto_activate path (do the socket work outside the lock)
        if getattr(self, "_unlocked_activate", False):
            self._unlocked_activate = False
            self.activate()

        if self.state == RECOVERING:
            self._flush_once()

    def _flush_once(self) -> None:
        """Primary is back — clear send backoff so the held studies forward now."""
        try:
            if not self.server.watcher.running:
                self.server.start_watcher()
            res = self.server.retry_stuck()
            if res.get("reset"):
                self.log.info(f"Emergency recovery — flushing {res['reset']} held instance(s) to primary",
                              kind="emergency")
        except Exception as exc:
            self.log.error(f"Emergency flush error: {exc}", kind="emergency")

    # ---- status ------------------------------------------------------------
    def _iso(self, t: float) -> str:
        if not t:
            return ""
        import datetime
        return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def status(self) -> dict:
        with self._lock:
            dests = []
            for d in self._trigger_dests():
                name = d.get("name", "")
                h = self._health.get(name)
                dests.append({
                    "name": name,
                    "online": bool(h.online) if h else True,
                    "offline_since": self._iso(h.offline_since) if (h and h.offline_since) else "",
                    "last_error": h.last_error if h else "",
                })
            prompt = (self.state == TRIGGERED and not self.prompt_dismissed)
            return {
                "armed": self.armed,
                "state": self.state,
                "active": self.active,
                "trigger_dest": self.trigger_dest,
                "since": self._iso(self.since),
                "prompt": prompt,
                "auto_activate": bool(self._cfg.get("auto_activate")),
                "monitored": len(dests),
                "destinations": dests,
            }
