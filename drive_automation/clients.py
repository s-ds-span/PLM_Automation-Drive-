"""HTTP clients for EV Simulator and Load Bank-2 dashboards."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import requests


class DashboardError(Exception):
    """Raised when a dashboard API call fails."""


def _pilot_reading_transient_error(reading: dict[str, Any]) -> str:
    """Return a non-empty error if ``/api/ev/pilot`` should be retried.

    After the EV Simulator moved PilotDingus from USB-CDC to FT232 UART, the
    HTTP JSON field names stayed the same, but ``error`` strings broadened
    (settling / retired / busy / no bytes). Treat any non-empty ``error`` as
    transient so we never accept a zeroed cache frame as a good sample.
    """
    err = str(reading.get("error") or "").strip()
    if err:
        return err
    # Cache-only sampler can briefly leave connected=false with empty error.
    if reading.get("connected") is False and not (
        reading.get("pwm") or float(reading.get("frequency_hz") or 0.0) > 1.0
    ):
        return "pilot not connected"
    return ""


class BaseClient:
    def __init__(self, base_url: str, operator: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.operator = operator
        self.password = password
        self.session = requests.Session()
        self.logged_in = False

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(
                method,
                url,
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise DashboardError(f"{method} {path} failed: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            err = data.get("error") or data.get("message") or resp.text
            if path == "/login" and resp.status_code == 401:
                raise DashboardError(
                    f"{method} {path} HTTP 401: {err} "
                    "(use the Pi SSH password for loadbank1/loadbank2, not the lab-cell password)"
                )
            raise DashboardError(f"{method} {path} HTTP {resp.status_code}: {err}")
        return data

    def live_status(self, timeout: float = 30.0) -> dict[str, Any]:
        return self._request("GET", "/api/public/live-status", timeout=timeout)

    def login(self, *, view_only: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "operator_name": self.operator,
            "password": self.password,
        }
        if view_only:
            body["view_only"] = True
        data = self._request("POST", "/login", json_body=body)
        self.logged_in = True
        return data

    def logout(self) -> dict[str, Any]:
        try:
            return self._request("POST", "/logout")
        finally:
            self.logged_in = False


class EvClient(BaseClient):
    def state(self) -> dict[str, Any]:
        return self._request("GET", "/api/state")

    def pilot(self, *, timeout: float = 30.0) -> dict[str, Any]:
        return self._request("GET", "/api/ev/pilot", timeout=timeout)

    def wait_ready(self, timeout_sec: float = 180.0, poll_sec: float = 2.0) -> dict[str, Any]:
        """Poll until EV session ready; fail fast if MODE_SELECT / startup erred."""
        deadline = time.time() + timeout_sec
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.state()
            status = str(last.get("startup_status") or "").strip().lower()
            if status == "error":
                err = (
                    str(last.get("startup_error") or "").strip()
                    or "Could not select EV contactor bank (MODE_SELECT)."
                )
                raise DashboardError(f"EV Simulator startup failed: {err}")
            if last.get("startup_session_ready"):
                return last
            if last.get("shutdown_status") == "running":
                raise DashboardError("EV Simulator is shutting down.")
            time.sleep(poll_sec)
        raise DashboardError("EV Simulator startup timed out.")

    def set_mode(self, mode: str) -> dict[str, Any]:
        return self._request("POST", "/api/ev/mode", json_body={"mode": mode})

    def set_mode_with_retry(
        self,
        mode: str,
        *,
        attempts: int = 3,
        delay_sec: float = 10.0,
        on_retry: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Set pilot mode; retry if PilotDingus serial is briefly unavailable.

        On HTTP 409 (loads still armed / current not near zero), Switch Off Loads
        first then retry — required by EV Simulator builds that no longer unload
        on mode change.
        """
        last_err: Optional[Exception] = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                return self.set_mode(mode)
            except DashboardError as exc:
                last_err = exc
                err = str(exc)
                if "HTTP 409" in err and mode.lower() in ("connect", "watch", "vent"):
                    try:
                        self.ensure_loads_off()
                    except DashboardError:
                        pass
                if attempt >= attempts:
                    break
                if on_retry:
                    on_retry(attempt, attempts, mode, err)
                time.sleep(delay_sec)
        raise DashboardError(
            f"Could not set pilot mode '{mode}' after {attempts} attempts: {last_err}"
        ) from last_err

    def pilot_with_retry(
        self,
        *,
        attempts: int = 3,
        delay_sec: float = 1.0,
        timeout: float = 8.0,
        on_retry: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Read pilot JSON; retry if UART/serial momentarily fails or times out.

        EV Simulator ``GET /api/ev/pilot`` is cache-only (background UART
        sampler). Transient FT232 dropouts still return HTTP 200 with an
        ``error`` field and zeroed duty/freq — those must be retried.
        """
        last_err: Optional[Exception] = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                reading = self.pilot(timeout=timeout)
                err = _pilot_reading_transient_error(reading)
                if err:
                    raise DashboardError(f"Pilot read error: {err}")
                return reading
            except DashboardError as exc:
                last_err = exc
                if attempt >= attempts:
                    break
                if on_retry:
                    on_retry(attempt, attempts, "pilot", str(exc))
                time.sleep(max(0.2, float(delay_sec)))
        raise DashboardError(
            f"Could not read pilot JSON after {attempts} attempts: {last_err}"
        ) from last_err

    def switch_loads(
        self,
        action: str,
        *,
        amps: Optional[int] = None,
        approach: str = "undershoot",
    ) -> dict[str, Any]:
        """Switch ON/OFF EV Simulator loads (``POST /api/ev/loads``).

        ``action="on"`` requires Charge mode; send ``amps`` (recommended).
        ``action="off"`` ramps to ~0 A (~6 A/s) — use a long timeout.

        ``approach="undershoot"`` asks the EV load algorithm to reach ``amps``
        from below only (never command above target). Exact amps are still
        used when a step combo exists (e.g. 44 A → 44 A). Older EV builds that
        ignore unknown fields keep previous behavior.
        """
        act = str(action or "").strip().lower()
        if act not in ("on", "off"):
            raise DashboardError(f"switch_loads action must be 'on' or 'off' (got {action!r})")
        body: dict[str, Any] = {"action": act}
        if amps is not None:
            body["amps"] = int(amps)
        mode = str(approach or "").strip().lower()
        if mode in ("undershoot", "monotonic", "never_overshoot"):
            body["approach"] = "undershoot"
            body["monotonic"] = True
            body["never_overshoot"] = True
        timeout = 180.0 if act == "off" else 30.0
        return self._request(
            "POST",
            "/api/ev/loads",
            json_body=body,
            timeout=timeout,
        )

    def retarget_loads(
        self,
        amps: int,
        *,
        approach: str = "undershoot",
    ) -> dict[str, Any]:
        """Re-apply Switch ON Loads at ``amps`` (clean undershoot contactor step).

        Used when pilot duty / advertised current changes so EV contactors snap
        to the new amp step instead of lagging with a soft ramp.
        """
        return self.switch_loads("on", amps=int(amps), approach=approach)

    def set_max_current(self, amps: int) -> dict[str, Any]:
        """Arm loads at ``amps`` via Switch ON Loads (Charge mode required).

        EV builds that no longer start drawing on max-current alone need this.
        For cap-only (no arm), use ``set_max_current_cap``.
        """
        return self.switch_loads("on", amps=int(amps), approach="undershoot")

    def set_max_current_cap(self, amps: int) -> dict[str, Any]:
        """Store max-current cap without arming loads (``POST /api/ev/max-current``)."""
        return self._request(
            "POST",
            "/api/ev/max-current",
            json_body={
                "amps": int(amps),
                "approach": "undershoot",
                "monotonic": True,
                "never_overshoot": True,
            },
        )

    def loads_armed(self) -> Optional[bool]:
        """Return ``loads_armed`` from ``/api/state``, or None if field absent."""
        try:
            st = self.state()
        except DashboardError:
            return None
        if "loads_armed" not in st:
            return None
        return bool(st.get("loads_armed"))

    def ensure_loads_off(self, *, timeout_sec: float = 180.0) -> dict[str, Any]:
        """Switch Off Loads and wait until ``loads_armed`` is false (best effort).

        Also tolerates 409 while already ramping — poll until clear or timeout.
        """
        last: dict[str, Any] = {}
        try:
            last = self.switch_loads("off")
        except DashboardError as exc:
            err = str(exc)
            # Already off / already ramping — continue to poll.
            if "HTTP 409" not in err and "HTTP 404" not in err:
                raise
            last = {"error": err, "ok": False}

        deadline = time.time() + float(timeout_sec)
        while time.time() < deadline:
            try:
                st = self.state()
            except DashboardError:
                time.sleep(1.0)
                continue
            armed = st.get("loads_armed")
            ramping = st.get("exit_charge_ramping")
            # Older EV builds without these fields: assume OFF call was enough.
            if armed is None and ramping is None:
                return last if last else {"ok": True, "loads_armed": False}
            if not bool(armed) and not bool(ramping):
                return {"ok": True, "loads_armed": False, **{k: st.get(k) for k in ("commanded_amps", "applied_amps") if k in st}}
            # Near-zero current is also acceptable.
            for key in ("commanded_amps", "applied_amps", "amps"):
                try:
                    if float(st.get(key) or 0) <= 0.5 and not bool(armed):
                        return {"ok": True, "loads_armed": False, key: st.get(key)}
                except (TypeError, ValueError):
                    pass
            time.sleep(1.0)
        raise DashboardError(
            "Timed out waiting for EV loads to switch off "
            f"(loads_armed still true after {timeout_sec:.0f}s)"
        )

    def test_new(self, charger_label: str, test_name: str, notes: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/ev/test/new",
            json_body={
                "charger_label": charger_label,
                "test_name": test_name,
                "notes": notes,
            },
        )

    def test_save(self) -> dict[str, Any]:
        return self._request("POST", "/api/ev/test/save")

    def test_export(self) -> tuple[bytes, str]:
        """Download the active EV test as ``.evtest.zip`` (must be logged in)."""
        url = f"{self.base_url}/api/ev/test/export"
        try:
            resp = self.session.get(url, timeout=120.0)
        except requests.RequestException as exc:
            raise DashboardError(f"GET /api/ev/test/export failed: {exc}") from exc
        if resp.status_code >= 400:
            try:
                data = resp.json()
                err = data.get("error") or data.get("message") or resp.text
            except ValueError:
                err = resp.text
            raise DashboardError(
                f"GET /api/ev/test/export HTTP {resp.status_code}: {err}"
            )
        filename = "ev_test.evtest.zip"
        cd = resp.headers.get("Content-Disposition") or ""
        # Content-Disposition: attachment; filename="Charger_Test.evtest.zip"
        if "filename=" in cd:
            part = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
            if part:
                filename = Path(part).name
        return resp.content, filename

    def safe_stop(self) -> dict[str, Any]:
        # Prefer Switch Off Loads before stop so teardown does not leave loads armed.
        try:
            self.ensure_loads_off(timeout_sec=60.0)
        except DashboardError:
            pass
        return self._request("POST", "/api/ev/stop")

    def wait_shutdown(self, timeout_sec: float = 120.0, poll_sec: float = 2.0) -> str:
        deadline = time.time() + timeout_sec
        status = "idle"
        while time.time() < deadline:
            try:
                state = self.state()
                status = str(state.get("shutdown_status") or "idle")
            except DashboardError as exc:
                err = str(exc)
                # POST /api/ev/stop often clears the session; further /api/state
                # polls return 401 not_logged_in. That means the unit is released.
                if "401" in err or "not_logged_in" in err.lower():
                    self.logged_in = False
                    return "done"
                raise
            if status == "done":
                return status
            if status == "error":
                err = self.state().get("shutdown_error") or "shutdown error"
                raise DashboardError(f"EV safe exit failed: {err}")
            time.sleep(poll_sec)
        raise DashboardError("EV safe exit timed out.")

    def pilot_snapshot(self, *, include_state: bool = True) -> dict[str, Any]:
        """Normalized pilot + charging fields for telemetry."""
        try:
            pilot = self.pilot(timeout=8.0)
        except DashboardError:
            pilot = {}
        state: dict[str, Any] = {}
        if include_state:
            try:
                state = self.state()
            except DashboardError:
                state = {}
        return {
            "frequency_hz": pilot.get("frequency_hz"),
            "duty_cycle_pct": pilot.get("duty_cycle_pct"),
            "cp_high_v": pilot.get("cp_voltage_v"),
            "cp_low_v": pilot.get("cp_low_v"),
            "adv_current_a": pilot.get("max_current_a"),
            "applied_current_a": state.get("applied_current_a"),
            "target_current_a": state.get("target_current_a"),
            "max_current_a": state.get("max_current_a"),
            "pilot_mode": pilot.get("pilot_mode") or (state.get("pilot") or {}).get("pilot_mode"),
            "status": state.get("status"),
        }

    def wait_pwm(
        self,
        timeout_sec: float = 60.0,
        poll_sec: float = 0.25,
        *,
        read_attempts: int = 3,
        read_delay_sec: float = 2.0,
        on_retry: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Wait until pilot reports PWM frequency (Hz > 1).

        Ignores cache frames that carry a UART/serial ``error`` (or disconnected
        with no PWM) so FT232 settle windows do not look like permanent no-PWM.
        """
        deadline = time.time() + timeout_sec
        last: dict[str, Any] = {}
        while time.time() < deadline:
            try:
                last = self.pilot()
                err = _pilot_reading_transient_error(last)
                if err:
                    raise DashboardError(f"Pilot read error: {err}")
            except DashboardError as exc:
                # Transient UART/serial loss — retry a few times, then keep waiting.
                if on_retry:
                    on_retry(1, read_attempts, "pilot", str(exc))
                try:
                    last = self.pilot_with_retry(
                        attempts=read_attempts,
                        delay_sec=read_delay_sec,
                        on_retry=on_retry,
                    )
                except DashboardError:
                    time.sleep(poll_sec)
                    continue
            freq = float(last.get("frequency_hz") or 0.0)
            if freq > 1.0 or last.get("pwm"):
                return last
            time.sleep(poll_sec)
        raise DashboardError("Timed out waiting for pilot PWM frequency.")


class Lb2Client(BaseClient):
    def state(self) -> dict[str, Any]:
        return self._request("GET", "/api/state")

    def wait_ready(self, timeout_sec: float = 180.0, poll_sec: float = 2.0) -> dict[str, Any]:
        deadline = time.time() + timeout_sec
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.state()
            if last.get("startup_ready") and last.get("controls_enabled"):
                return last
            if last.get("shutdown_status") == "running":
                raise DashboardError("Load Bank-2 is shutting down.")
            time.sleep(poll_sec)
        raise DashboardError("Load Bank-2 startup timed out.")

    @staticmethod
    def _voltage_token(value: str) -> str:
        """Normalize UI/API voltage to LB2 tokens: OFF / 120 / 240."""
        text = str(value or "").strip().upper().replace("V", "")
        if text == "OFF":
            return "OFF"
        if text in ("120", "240"):
            return text
        return text

    def read_channel_voltage(self, channel: int) -> Optional[str]:
        """Return ``120`` / ``240`` / ``OFF`` from LB2 ``/api/state``, if present."""
        try:
            raw = (self.state().get("voltage") or {})
        except DashboardError:
            return None
        if not isinstance(raw, dict):
            return None
        val = raw.get(channel)
        if val is None:
            val = raw.get(str(channel))
        if val is None:
            return None
        return self._voltage_token(str(val))

    def set_voltage(self, channel: int, value: str) -> dict[str, Any]:
        """Set one LB2 channel voltage.

        Hardware boots channels at 240 V. Switching 240↔120 is ignored unless
        the channel is OFF first and ``confirmed=True`` is sent (LB2 over-voltage
        confirm). Read back and fail if the LCD/state does not match the UI.
        """
        wanted = self._voltage_token(value)
        if wanted == "OFF":
            return self._request(
                "POST",
                "/api/voltage",
                json_body={"channel": int(channel), "value": "OFF"},
            )

        # Relays + voltage OFF first — LB2 will not switch 240→120 with loads on.
        try:
            self.apply_channel_loads(int(channel), set())
        except Exception:
            pass
        self._request(
            "POST",
            "/api/voltage",
            json_body={"channel": int(channel), "value": "OFF"},
        )
        time.sleep(0.4)

        body = {"channel": int(channel), "value": wanted}
        result = self._request("POST", "/api/voltage", json_body=body)
        # Always confirm: some builds omit need_confirm but still require it.
        result = self._request(
            "POST",
            "/api/voltage",
            json_body={**body, "confirmed": True},
        )
        time.sleep(0.3)
        actual = self.read_channel_voltage(int(channel))
        if actual and actual != wanted:
            # Alternate token used by some LB2 images (``120V``).
            alt = f"{wanted}V"
            result = self._request(
                "POST",
                "/api/voltage",
                json_body={"channel": int(channel), "value": alt, "confirmed": True},
            )
            actual = self.read_channel_voltage(int(channel))
        if actual and actual != wanted:
            raise DashboardError(
                f"LB2 CH{channel} voltage stayed {actual} V after requesting {wanted} V "
                "(LCD would still show the old voltage — check LB2 confirm / channel lock)"
            )
        return result

    def toggle_load(self, channel: int, load: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/load",
            json_body={"channel": channel, "load": load},
        )

    def safe_shutdown(self, reason: str = "Drive automation complete") -> dict[str, Any]:
        return self._request("POST", "/api/shutdown", json_body={"reason": reason})

    def wait_shutdown(self, timeout_sec: float = 120.0, poll_sec: float = 2.0) -> str:
        deadline = time.time() + timeout_sec
        status = "idle"
        while time.time() < deadline:
            status = str(self.state().get("shutdown_status") or "idle")
            if status == "done":
                return status
            if status == "error":
                err = self.state().get("shutdown_error") or "shutdown error"
                raise DashboardError(f"LB2 safe exit failed: {err}")
            time.sleep(poll_sec)
        raise DashboardError("LB2 safe exit timed out.")

    def meter_snapshot(self) -> dict[str, Any]:
        try:
            state = self.state()
        except DashboardError:
            return {}
        meters = state.get("meters") or {}
        total_w = 0.0
        total_a = 0.0
        for _ch, m in meters.items():
            if not isinstance(m, dict):
                continue
            try:
                total_w += float(m.get("P") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                total_a += float(m.get("I") or 0.0)
            except (TypeError, ValueError):
                pass
        return {
            "voltages": state.get("voltage") or {},
            "meters": meters,
            "loads": {k: v for k, v in (state.get("loads") or {}).items() if v},
            "status": state.get("status"),
            "total_watts": round(total_w, 1),
            "total_amps": round(total_a, 2),
        }

    def current_load_state(self) -> dict[int, set[str]]:
        from drive_automation.load_selection import parse_load_state

        loads = self.state().get("loads") or {}
        return parse_load_state(loads)

    def apply_channel_loads(self, channel: int, desired: set[str]) -> None:
        from drive_automation.load_selection import all_load_keys

        # One state read up front — toggling each relay used to re-fetch /api/state
        # and could stall for minutes when many steps change at high commanded amps.
        current = self.current_load_state().get(channel, set())
        keys = list(all_load_keys(channel))
        # Clean step: turn OFF extras first (never overshoot), then turn ON needed.
        # Avoids intermediate overshoot when swapping large/small steps.
        to_off = [load for load in keys if load in current and load not in desired]
        to_on = [load for load in keys if load in desired and load not in current]
        for load in to_off:
            self.toggle_load(channel, load)
        for load in to_on:
            self.toggle_load(channel, load)

    def apply_panel_loads(
        self,
        desired: dict[int, set[str]],
        *,
        panel_channels: tuple[int, ...] | None = None,
    ) -> None:
        """Apply multi-channel loads (per-channel toggle of changed steps only)."""
        channels = panel_channels or tuple(sorted(desired.keys()))
        for ch in channels:
            self.apply_channel_loads(ch, desired.get(ch, set()))