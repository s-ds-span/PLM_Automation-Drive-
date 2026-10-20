"""Structured JSON-line logging for automation dashboard consumption."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from drive_automation.timeutil import stamp_fields

if TYPE_CHECKING:
    from drive_automation.clients import EvClient, Lb2Client


# Events that belong in the dedicated pilot capture jsonl.
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


class TelemetryLogger:
    """Emit pilot + LB2 snapshots on a fixed interval (default 500 ms)."""

    def __init__(
        self,
        interval_sec: float = 0.5,
        log_path: Optional[str] = None,
        *,
        pilot_log_path: Optional[str | Path] = None,
        tz_name: Optional[str] = None,
    ) -> None:
        self.interval_sec = interval_sec
        self._default_interval_sec = float(interval_sec)
        self.log_path = log_path
        self.pilot_log_path = Path(pilot_log_path) if pilot_log_path else None
        self.tz_name = tz_name
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ev: Optional[EvClient] = None
        self._lb2: Optional[Lb2Client] = None
        self._extra: Callable[[], dict[str, Any]] = lambda: {}
        self._emit_fn: Optional[Callable[[dict[str, Any]], None]] = None
        self._lock = threading.Lock()
        self._pilot_lock = threading.Lock()
        self._poll_ev = True
        self._poll_lb2 = True
        # When False, EV telemetry uses pilot-only (skips /api/state) to cut load.
        self._ev_include_state = True

    def set_pilot_log_path(self, path: Optional[str | Path]) -> None:
        self.pilot_log_path = Path(path) if path else None

    def set_tz_name(self, tz_name: Optional[str]) -> None:
        self.tz_name = tz_name

    def bind(
        self,
        ev: Optional[EvClient],
        lb2: Optional[Lb2Client],
        extra: Optional[Callable[[], dict[str, Any]]] = None,
        emit_fn: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> None:
        self._ev = ev
        self._lb2 = lb2
        if extra is not None:
            self._extra = extra
        self._emit_fn = emit_fn

    def set_interval(self, interval_sec: float) -> None:
        """Change poll period for the running loop (thread-safe)."""
        with self._lock:
            self.interval_sec = max(0.2, float(interval_sec))

    def set_polling(
        self,
        *,
        ev: Optional[bool] = None,
        lb2: Optional[bool] = None,
        ev_include_state: Optional[bool] = None,
    ) -> None:
        """Enable/disable EV / LB2 polling independently (thread-safe)."""
        with self._lock:
            if ev is not None:
                self._poll_ev = bool(ev)
            if lb2 is not None:
                self._poll_lb2 = bool(lb2)
            if ev_include_state is not None:
                self._ev_include_state = bool(ev_include_state)

    def lighten_for_load_control(self) -> None:
        """Minimize EV HTTP load during aggregate/throttle/recover.

        Duty / applied samples come from explicit aggregation reads; background
        EV pilot+state every 0.5s contended with those and caused timeouts.
        """
        self.set_interval(30.0)
        self.set_polling(ev=False, lb2=True, ev_include_state=False)

    def restore_default_polling(self) -> None:
        self.set_interval(self._default_interval_sec)
        self.set_polling(ev=True, lb2=True, ev_include_state=True)

    def _append_pilot_capture(self, record: dict[str, Any]) -> None:
        """Write pilot-relevant rows to the dedicated pilot jsonl (if configured)."""
        path = self.pilot_log_path
        if path is None:
            return
        event = str(record.get("event") or "")
        if event not in _PILOT_EVENTS:
            return
        if event == "telemetry" and not record.get("ev"):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str)
        with self._pilot_lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()

    def emit(self, event: str, **payload: Any) -> None:
        stamps = stamp_fields(self.tz_name)
        record = {
            **stamps,
            "event": event,
            **payload,
        }
        # Preserve Modbus-line timestamps from abnormality hits when provided.
        if payload.get("ts") and event in ("sbx_abnormality",):
            record["log_ts"] = payload.get("ts")
            record["ts"] = stamps["ts"]
            record["ts_local"] = stamps["ts_local"]
        line = json.dumps(record, default=str)
        print(line, flush=True)
        if self._emit_fn:
            # Dashboard/CLI owns the run jsonl — avoid duplicate lines.
            self._emit_fn(record)
        elif self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._append_pilot_capture(record)
        else:
            self._append_pilot_capture(record)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                interval = float(self.interval_sec)
                poll_ev = self._poll_ev
                poll_lb2 = self._poll_lb2
                ev_state = self._ev_include_state
            extra = self._extra()
            payload: dict[str, Any] = {"phase": extra.get("phase", "")}
            if poll_ev and self._ev and self._ev.logged_in:
                try:
                    if ev_state:
                        payload["ev"] = self._ev.pilot_snapshot()
                    else:
                        payload["ev"] = self._ev.pilot_snapshot(include_state=False)
                except Exception as exc:
                    payload["ev_error"] = str(exc)
            if poll_lb2 and self._lb2 and self._lb2.logged_in:
                try:
                    payload["lb2"] = self._lb2.meter_snapshot()
                except Exception as exc:
                    payload["lb2_error"] = str(exc)
            payload.update({k: v for k, v in extra.items() if k != "phase"})
            if extra.get("phase"):
                payload["phase"] = extra["phase"]
            self.emit("telemetry", **payload)
            self._stop.wait(interval)


def append_pilot_capture_line(
    path: Optional[str | Path],
    record: dict[str, Any],
) -> None:
    """Append one pilot-relevant record to ``path`` (no-op if not applicable)."""
    if path is None:
        return
    event = str(record.get("event") or "")
    if event not in _PILOT_EVENTS:
        return
    if event == "telemetry" and not record.get("ev"):
        return
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
        fh.flush()
