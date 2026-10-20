"""Multi-stream lab capture: pilot, EV loads, LB2 meters, gateway panel current.

Used by:
- automation runs (append from emitted events + feeder samples)
- manual dashboard capture while operators drive EV/LB2 by hand
"""

from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

from drive_automation.timeutil import now_local, stamp_fields

STREAMS = ("pilot", "ev_loads", "lb2", "panel")

_PILOT_EVENTS = frozenset(
    {
        "telemetry",
        "pilot_mode",
        "pilot_pwm",
        "pilot_charge",
        "pilot_duty_sample",
        "pilot_retry",
        "pilot_modes_done",
        "duty_vs_charge_rate",
        "duty_settle_start",
        "duty_settle_ok",
        "duty_settle_timeout",
        "ev_charging",
    }
)


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
        fh.flush()


class LabStreamCapture:
    """Write separate jsonl files for pilot / EV loads / LB2 / panel currents."""

    def __init__(
        self,
        session_dir: Path,
        *,
        session_id: str,
        tz_name: Optional[str] = None,
        enabled: Optional[set[str]] = None,
    ) -> None:
        self.session_dir = Path(session_dir)
        self.session_id = str(session_id)
        self.tz_name = tz_name
        self.enabled = set(enabled or STREAMS)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.paths: dict[str, Path] = {
            "pilot": self.session_dir / f"{self.session_id}_pilot.jsonl",
            "ev_loads": self.session_dir / f"{self.session_id}_ev_loads.jsonl",
            "lb2": self.session_dir / f"{self.session_id}_lb2.jsonl",
            "panel": self.session_dir / f"{self.session_id}_panel.jsonl",
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ev = None
        self._lb2 = None
        self._gateway = None
        self.interval_sec = 1.0
        self.mode = "idle"  # idle | automation | manual
        self.started_at: Optional[str] = None
        self.last: dict[str, Any] = {
            "pilot": None,
            "ev_loads": None,
            "lb2": None,
            "panel": None,
            "errors": {},
        }

    def path_for(self, stream: str) -> Optional[Path]:
        if stream not in self.enabled:
            return None
        return self.paths.get(stream)

    def existing_files(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, path in self.paths.items():
            if name in self.enabled and path.is_file() and path.stat().st_size > 0:
                out[name] = str(path)
        return out

    def append_record(self, stream: str, record: dict[str, Any]) -> None:
        if stream not in self.enabled:
            return
        path = self.paths.get(stream)
        if path is None:
            return
        with self._lock:
            _append_jsonl(path, record)
            self.last[stream] = record

    def ingest_event(self, record: dict[str, Any]) -> None:
        """Route an automation/SSE event into the matching stream file(s)."""
        event = str(record.get("event") or "")
        stamps = {
            k: record[k]
            for k in ("ts", "ts_local", "tz")
            if record.get(k) is not None
        }
        if not stamps:
            stamps = stamp_fields(self.tz_name)

        if event in _PILOT_EVENTS and (event != "telemetry" or record.get("ev")):
            self.append_record(
                "pilot",
                {
                    **stamps,
                    "event": event,
                    "source": "event",
                    **{
                        k: v
                        for k, v in record.items()
                        if k not in ("ts", "ts_local", "tz", "event")
                    },
                },
            )

        if event == "telemetry" and record.get("ev"):
            ev = record.get("ev") or {}
            self.append_record(
                "ev_loads",
                {
                    **stamps,
                    "event": "ev_loads",
                    "source": "telemetry",
                    "applied_current_a": ev.get("applied_current_a"),
                    "target_current_a": ev.get("target_current_a"),
                    "max_current_a": ev.get("max_current_a"),
                    "adv_current_a": ev.get("adv_current_a"),
                    "duty_cycle_pct": ev.get("duty_cycle_pct"),
                    "pilot_mode": ev.get("pilot_mode"),
                    "status": ev.get("status"),
                },
            )

        if event == "telemetry" and record.get("lb2"):
            lb2 = record.get("lb2") or {}
            self.append_record(
                "lb2",
                {
                    **stamps,
                    "event": "lb2_meters",
                    "source": "telemetry",
                    "total_amps": lb2.get("total_amps"),
                    "total_watts": lb2.get("total_watts"),
                    "voltages": lb2.get("voltages"),
                    "meters": lb2.get("meters"),
                    "loads": lb2.get("loads"),
                    "status": lb2.get("status"),
                },
            )

        if event == "feeder_amps":
            self.append_record(
                "panel",
                {
                    **stamps,
                    "event": "panel_current",
                    "source": "gateway_feeder",
                    "combined_a": record.get("combined_a"),
                    "line_a_a": record.get("line_a_a"),
                    "line_b_a": record.get("line_b_a"),
                    "message": record.get("message"),
                },
            )

    def start_manual_polling(
        self,
        *,
        ev=None,
        lb2=None,
        gateway=None,
        interval_sec: float = 1.0,
    ) -> None:
        """Background poll for manual lab operation (operator drives units by hand)."""
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Lab capture already running.")
        self._ev = ev
        self._lb2 = lb2
        self._gateway = gateway
        self.interval_sec = max(0.5, float(interval_sec))
        self.mode = "manual"
        self.started_at = stamp_fields(self.tz_name)["ts"]
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="lab-stream-capture", daemon=True
        )
        self._thread.start()

    def attach_automation(self) -> None:
        """Mark session as automation-fed (no internal poller)."""
        self.mode = "automation"
        if self.started_at is None:
            self.started_at = stamp_fields(self.tz_name)["ts"]

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=5.0)
        self._thread = None
        if self.mode == "manual":
            self.mode = "idle"

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive()) or self.mode == "automation"

    def status(self) -> dict[str, Any]:
        files = self.existing_files()
        sizes = {
            name: (self.paths[name].stat().st_size if self.paths[name].is_file() else 0)
            for name in self.enabled
            if name in self.paths
        }
        return {
            "ok": True,
            "session_id": self.session_id,
            "session_dir": str(self.session_dir),
            "mode": self.mode,
            "running": self.running(),
            "started_at": self.started_at,
            "enabled": sorted(self.enabled),
            "files": files,
            "sizes": sizes,
            "last": {
                "pilot": self._brief(self.last.get("pilot")),
                "ev_loads": self._brief(self.last.get("ev_loads")),
                "lb2": self._brief(self.last.get("lb2")),
                "panel": self._brief(self.last.get("panel")),
                "errors": dict(self.last.get("errors") or {}),
            },
            "download_urls": {
                name: f"/api/lab-capture/download?stream={name}"
                for name in files
            },
            "zip_url": "/api/lab-capture/download?stream=zip",
        }

    @staticmethod
    def _brief(row: Any) -> Any:
        if not isinstance(row, dict):
            return row
        # Manual pilot samples nest readings under ``ev``; flatten for the UI.
        nested = row.get("ev") if isinstance(row.get("ev"), dict) else {}
        merged = {**nested, **{k: v for k, v in row.items() if k != "ev"}}
        keep = (
            "ts",
            "ts_local",
            "event",
            "applied_current_a",
            "target_current_a",
            "max_current_a",
            "adv_current_a",
            "total_amps",
            "combined_a",
            "line_a_a",
            "line_b_a",
            "duty_cycle_pct",
            "pilot_mode",
            "status",
        )
        return {k: merged[k] for k in keep if k in merged and merged[k] is not None}

    def build_zip(self, dest: Optional[Path] = None) -> Path:
        files = self.existing_files()
        if not files:
            raise FileNotFoundError("No lab capture files to zip.")
        out = dest or (self.session_dir / f"{self.session_id}_lab_capture.zip")
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, path in files.items():
                zf.write(path, arcname=Path(path).name)
        return out

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            stamps = stamp_fields(self.tz_name)
            errors: dict[str, str] = {}

            if "pilot" in self.enabled or "ev_loads" in self.enabled:
                if self._ev is not None and getattr(self._ev, "logged_in", False):
                    try:
                        snap = self._ev.pilot_snapshot(include_state=True)
                        if "pilot" in self.enabled:
                            self.append_record(
                                "pilot",
                                {
                                    **stamps,
                                    "event": "pilot_sample",
                                    "source": "manual_poll",
                                    "ev": snap,
                                },
                            )
                        if "ev_loads" in self.enabled:
                            self.append_record(
                                "ev_loads",
                                {
                                    **stamps,
                                    "event": "ev_loads",
                                    "source": "manual_poll",
                                    "applied_current_a": snap.get("applied_current_a"),
                                    "target_current_a": snap.get("target_current_a"),
                                    "max_current_a": snap.get("max_current_a"),
                                    "adv_current_a": snap.get("adv_current_a"),
                                    "duty_cycle_pct": snap.get("duty_cycle_pct"),
                                    "pilot_mode": snap.get("pilot_mode"),
                                    "status": snap.get("status"),
                                },
                            )
                    except Exception as exc:
                        errors["ev"] = str(exc)
                else:
                    errors["ev"] = "EV not logged in"

            if "lb2" in self.enabled:
                if self._lb2 is not None and getattr(self._lb2, "logged_in", False):
                    try:
                        meter = self._lb2.meter_snapshot()
                        self.append_record(
                            "lb2",
                            {
                                **stamps,
                                "event": "lb2_meters",
                                "source": "manual_poll",
                                "total_amps": meter.get("total_amps"),
                                "total_watts": meter.get("total_watts"),
                                "voltages": meter.get("voltages"),
                                "meters": meter.get("meters"),
                                "loads": meter.get("loads"),
                                "status": meter.get("status"),
                            },
                        )
                    except Exception as exc:
                        errors["lb2"] = str(exc)
                else:
                    errors["lb2"] = "LB2 not logged in"

            if "panel" in self.enabled:
                if self._gateway is not None and getattr(self._gateway, "available", False):
                    try:
                        from drive_automation.panel_ratings import sample_feeder_amps

                        sample = sample_feeder_amps(
                            self._gateway,
                            timeout_s=min(8.0, max(3.0, self.interval_sec + 2.0)),
                            quiet=True,
                        )
                        if sample:
                            self.append_record(
                                "panel",
                                {
                                    **stamps,
                                    "event": "panel_current",
                                    "source": "manual_poll",
                                    "combined_a": sample.get("combined_a"),
                                    "line_a_a": sample.get("line_a_a"),
                                    "line_b_a": sample.get("line_b_a"),
                                },
                            )
                        else:
                            errors["panel"] = "No feeder sample"
                    except Exception as exc:
                        errors["panel"] = str(exc)
                else:
                    errors["panel"] = "Gateway not available"

            self.last["errors"] = errors
            self._stop.wait(self.interval_sec)


# Process-wide manual capture session (dashboard).
_manual_lock = threading.Lock()
_manual_capture: Optional[LabStreamCapture] = None
_manual_clients: dict[str, Any] = {"ev": None, "lb2": None, "gateway": None}


def get_manual_capture() -> Optional[LabStreamCapture]:
    return _manual_capture


def start_manual_lab_capture(
    *,
    log_root: Path,
    tz_name: Optional[str],
    streams: Optional[list[str]] = None,
    interval_sec: float = 1.0,
    login_ev: bool = True,
    login_lb2: bool = True,
    use_gateway: bool = True,
    take_control: bool = False,
) -> dict[str, Any]:
    """Start manual multi-stream capture using config credentials.

    By default logins are ``view_only`` so EV/LB2 contactors and loads are not
    claimed or started. Pass ``take_control=True`` for an exclusive operator
    session (still does not Switch ON loads — capture is meter/pilot only).
    """
    global _manual_capture, _manual_clients

    from drive_automation.clients import EvClient, Lb2Client
    from drive_automation.config import DriveConfig
    from drive_automation.gateway import build_gateway_shell_from_env

    with _manual_lock:
        if _manual_capture is not None and _manual_capture.running():
            return {
                "ok": False,
                "error": "Manual lab capture already running.",
                **_manual_capture.status(),
            }

        cfg = DriveConfig.from_env()
        enabled = set(streams or STREAMS) & set(STREAMS)
        if not enabled:
            return {"ok": False, "error": "Select at least one stream."}

        stamp = now_local(tz_name or cfg.drive_timezone).strftime("%Y%m%d_%H%M%S")
        session_id = f"manual_{stamp}"
        session_dir = Path(log_root) / "lab_capture" / session_id
        cap = LabStreamCapture(
            session_dir,
            session_id=session_id,
            tz_name=tz_name or cfg.drive_timezone,
            enabled=enabled,
        )

        ev = None
        lb2 = None
        gateway = None
        notes: list[str] = []
        view_only = not bool(take_control)
        login_mode = "exclusive control" if take_control else "view-only (loads unchanged)"
        notes.append(f"Login mode: {login_mode}")

        if login_ev and enabled & {"pilot", "ev_loads"}:
            try:
                ev = EvClient(cfg.ev_url, cfg.operator, cfg.ev_password)
                ev.login(view_only=view_only)
                notes.append(
                    "EV logged in (view-only)" if view_only else "EV logged in (exclusive)"
                )
            except Exception as exc:
                notes.append(f"EV login failed: {exc}")
                ev = None

        if login_lb2 and "lb2" in enabled:
            try:
                lb2 = Lb2Client(cfg.lb2_url, cfg.operator, cfg.lb2_password)
                lb2.login(view_only=view_only)
                notes.append(
                    "LB2 logged in (view-only)" if view_only else "LB2 logged in (exclusive)"
                )
            except Exception as exc:
                notes.append(f"LB2 login failed: {exc}")
                lb2 = None

        if use_gateway and "panel" in enabled:
            try:
                gateway = build_gateway_shell_from_env()
                if not gateway.available:
                    notes.append("Gateway SSH not available")
                    gateway = None
                else:
                    notes.append("Gateway ready")
            except Exception as exc:
                notes.append(f"Gateway failed: {exc}")
                gateway = None

        cap.start_manual_polling(
            ev=ev, lb2=lb2, gateway=gateway, interval_sec=interval_sec
        )
        _manual_capture = cap
        _manual_clients = {"ev": ev, "lb2": lb2, "gateway": gateway}
        status = cap.status()
        status["message"] = "Manual lab capture started"
        status["notes"] = notes
        status["take_control"] = bool(take_control)
        status["view_only"] = view_only
        return status


def stop_manual_lab_capture(*, logout: bool = True) -> dict[str, Any]:
    global _manual_capture, _manual_clients
    with _manual_lock:
        cap = _manual_capture
        clients = dict(_manual_clients)
        if cap is None:
            return {"ok": True, "running": False, "message": "No manual capture active."}
        cap.stop()
        if logout:
            for key in ("ev", "lb2"):
                client = clients.get(key)
                if client is None:
                    continue
                try:
                    if hasattr(client, "logout"):
                        client.logout()
                except Exception:
                    pass
        status = cap.status()
        status["message"] = "Manual lab capture stopped"
        status["ok"] = True
        # Keep files downloadable after stop.
        _manual_clients = {"ev": None, "lb2": None, "gateway": None}
        return status


def manual_capture_status() -> dict[str, Any]:
    cap = _manual_capture
    if cap is None:
        return {"ok": True, "running": False, "mode": "idle", "files": {}}
    return cap.status()
