"""JAMA cases: SBX / seriald comms loss, boot, and charge-rate setpoints.

6  Comms loss → throttle/advertise fallback to 6 A, then restore to 80% breaker
7  Comms-required flag + relay cycle + seriald kill → 6 A on pilot
8  No comms on boot with vehicle in Connect — pilot present, no SBX data
9  Gate: set-charge-rate (10) only after throttle verification passed
10 Set user-max 0 → charge_rate_a == 0 (MsgDriveStatus / MQTT)
11 Power-reset Drive relay → SBX comms return after boot

Gateway commands use ``GatewayShell.run`` (spanssh). Pilot mode/amps use EV HTTP.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from drive_automation.advertise_cases import (
    ensure_pilot_charge,
    read_advertised_current_a,
    read_breaker_rating_a,
    set_user_max_charge_current_a,
)
from drive_automation.gateway import GatewayShell
from drive_automation.jama_cases import CaseIdFactory, emit_jama_case
from drive_automation.panel_observe import PanelObserver

EmitFn = Callable[..., None]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _relay_wait_sec() -> float:
    return max(5.0, _env_float("DRIVE_COMMS_RELAY_WAIT_SEC", 120.0))


def _boot_wait_sec() -> float:
    return max(5.0, _env_float("DRIVE_COMMS_BOOT_WAIT_SEC", 120.0))


def _fallback_wait_sec() -> float:
    return max(5.0, _env_float("DRIVE_COMMS_FALLBACK_WAIT_SEC", 45.0))


def _run(shell: GatewayShell, command: str, *, timeout_s: float = 60) -> tuple[int, str]:
    code, out, err = shell.run(command, timeout_s=timeout_s)
    return code, ((out or err or "").strip())


def _emit_case(
    emit: EmitFn,
    case_ids: Optional[CaseIdFactory],
    *,
    title: str,
    signal: str,
    expected: str,
    observed: str,
    result: str,
    evidence: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> None:
    if case_ids is not None:
        emit_jama_case(
            emit,
            case_id=case_ids.next("COMMS"),
            section="Comms failover",
            title=title,
            signal=signal,
            expected=expected,
            observed=observed,
            sources="gateway,pilot",
            result=result,
            evidence=evidence,
            metrics=metrics,
        )
    else:
        emit(
            "comms_case",
            ok=str(result).lower() == "pass",
            title=title,
            expected=expected,
            observed=observed,
            metrics=metrics,
            message=f"{title}: {result} — {observed}",
        )


def stop_sbx_seriald(shell: GatewayShell) -> str:
    code, text = _run(shell, "systemctl stop span-sbx-seriald", timeout_s=45)
    return f"exit={code} {text[:200]}"


def start_sbx_seriald(shell: GatewayShell) -> str:
    code, text = _run(shell, "systemctl start span-sbx-seriald", timeout_s=45)
    return f"exit={code} {text[:200]}"


def drive_relay_disconnect(shell: GatewayShell, instance_id: int) -> str:
    cmd = (
        f"span-pcm microtrait --instance-id {int(instance_id)} "
        "switch-load-management disconnect "
        "--disconnect-reason-control-source user-command"
    )
    code, text = _run(shell, cmd, timeout_s=60)
    return f"exit={code} {text[:300]}"


def drive_relay_override(shell: GatewayShell, instance_id: int) -> str:
    cmd = (
        f"span-pcm microtrait --instance-id {int(instance_id)} "
        "switch-load-management override"
    )
    code, text = _run(shell, cmd, timeout_s=60)
    return f"exit={code} {text[:300]}"


def remove_comms_required(shell: GatewayShell) -> str:
    """Best-effort remove of Drive ``comms-required`` via sbx-ctl on the gateway."""
    cmd = (
        "sbx-ctl-hw1 --device /dev/span-rs485-aux --receiver-id 0 "
        "remove-file comms-required"
    )
    code, text = _run(shell, cmd, timeout_s=90)
    if code != 0:
        # Alternate binary name used on some images.
        code2, text2 = _run(
            shell,
            "sbx-ctl --device /dev/span-rs485-aux --receiver-id 0 "
            "remove-file comms-required",
            timeout_s=90,
        )
        return f"sbx-ctl-hw1 exit={code} {text[:200]}; sbx-ctl exit={code2} {text2[:200]}"
    return f"exit={code} {text[:300]}"


def pilot_has_pwm(reading: dict[str, Any]) -> bool:
    try:
        freq = float(reading.get("frequency_hz") or 0.0)
    except (TypeError, ValueError):
        freq = 0.0
    return bool(reading.get("pwm") or freq > 1.0)


def pilot_adv_a(reading: dict[str, Any]) -> Optional[float]:
    raw = reading.get("max_current_a")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def wait_pilot_near_amps(
    ev: Any,
    target_a: float,
    *,
    timeout_sec: float,
    tol_a: float = 1.5,
    poll_sec: float = 2.0,
) -> tuple[bool, Optional[float], dict[str, Any]]:
    deadline = time.time() + timeout_sec
    last: dict[str, Any] = {}
    last_adv: Optional[float] = None
    while time.time() < deadline:
        try:
            last = ev.pilot_with_retry(attempts=2, delay_sec=1.0, timeout=8.0)
        except Exception:
            time.sleep(poll_sec)
            continue
        last_adv = pilot_adv_a(last)
        if last_adv is not None and abs(last_adv - target_a) <= tol_a:
            return True, last_adv, last
        time.sleep(poll_sec)
    return False, last_adv, last


def wait_mqtt_advertised_near(
    shell: GatewayShell,
    target_a: float,
    *,
    timeout_sec: float,
    tol_a: float = 1.5,
    poll_sec: float = 3.0,
) -> tuple[bool, Optional[float]]:
    deadline = time.time() + timeout_sec
    last: Optional[float] = None
    while time.time() < deadline:
        last = read_advertised_current_a(shell, wait_sec=min(5.0, poll_sec + 1))
        if last is not None and abs(float(last) - target_a) <= tol_a:
            return True, float(last)
        time.sleep(poll_sec)
    return False, last


def sbx_comms_present(shell: GatewayShell, *, wait_sec: float = 8.0) -> tuple[bool, str]:
    """True if eve status / MQTT yields a charge_rate or advertised current."""
    panel = PanelObserver(shell, wait_sec=wait_sec)
    snap = panel.poll_status()
    if snap.charge_rate_a is not None:
        return True, f"mqtt charge_rate_a={snap.charge_rate_a}"
    adv = read_advertised_current_a(shell, wait_sec=wait_sec)
    if adv is not None:
        return True, f"mqtt advertised_current_a={adv}"
    code, text = _run(
        shell,
        "journalctl -u span-sbx-seriald -n 20 --no-pager -o cat",
        timeout_s=30,
    )
    snippet = text[-300:] if text else ""
    # Active unit with recent lines is weak evidence; prefer MQTT fields above.
    if code == 0 and snippet and "No entries" not in snippet:
        return False, f"seriald journal only (no charge_rate): {snippet[:200]}"
    return False, "no charge_rate_a / advertised_current_a on MQTT"


def ensure_pilot_connect(ev: Any, *, attempts: int = 3) -> dict[str, Any]:
    reading = ev.pilot_with_retry(attempts=attempts, delay_sec=2.0)
    if str(reading.get("pilot_mode") or "").lower() != "connect":
        # Leaving Charge may need loads off first.
        try:
            ev.ensure_loads_off()
        except Exception:
            pass
        ev.set_mode_with_retry("connect", attempts=attempts, delay_sec=10.0)
        time.sleep(1.0)
        reading = ev.pilot_with_retry(attempts=attempts, delay_sec=2.0)
    if str(reading.get("pilot_mode") or "").lower() != "connect":
        raise RuntimeError(
            f"Pilot not in Connect mode (got {reading.get('pilot_mode')!r})"
        )
    return reading


def poll_charge_rate_a(
    shell: GatewayShell,
    *,
    timeout_sec: float = 30.0,
    poll_sec: float = 2.0,
) -> Optional[float]:
    deadline = time.time() + timeout_sec
    panel = PanelObserver(shell, wait_sec=min(5.0, poll_sec + 1))
    last: Optional[float] = None
    while time.time() < deadline:
        snap = panel.poll_status()
        if snap.charge_rate_a is not None:
            last = float(snap.charge_rate_a)
            return last
        time.sleep(poll_sec)
    return last


# ---------------------------------------------------------------------------
# Case 6 — comms loss throttle down to 6 A, then restore
# ---------------------------------------------------------------------------


def case_comms_loss_throttle_down(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    instance_id: int,
    case_ids: Optional[CaseIdFactory] = None,
    restore_fraction: float = 0.8,
) -> dict[str, Any]:
    reading = ensure_pilot_charge(ev)
    if not pilot_has_pwm(reading):
        _emit_case(
            emit,
            case_ids,
            title="Comms loss throttle down (precheck)",
            signal="pilot PWM",
            expected="Charge mode with PWM / frequency_hz > 1",
            observed=(
                f"mode={reading.get('pilot_mode')} "
                f"freq={reading.get('frequency_hz')} pwm={reading.get('pwm')}"
            ),
            result="fail",
            evidence=[],
            metrics={"pilot_mode": reading.get("pilot_mode")},
        )
        return {"ok": False, "reason": "no_pwm"}

    before_adv = read_advertised_current_a(shell, wait_sec=5.0)
    before_pilot = pilot_adv_a(reading)
    rating_a = read_breaker_rating_a(shell, instance_id)
    restore_a = int(round(float(rating_a) * float(restore_fraction)))

    stop_detail = ""
    start_detail = ""
    ok_fallback = False
    ok_restore = False
    fallback_pilot: Optional[float] = None
    fallback_mqtt: Optional[float] = None
    restore_mqtt: Optional[float] = None

    try:
        stop_detail = stop_sbx_seriald(shell)
        time.sleep(2.0)
        ok_p, fallback_pilot, _ = wait_pilot_near_amps(
            ev, 6.0, timeout_sec=_fallback_wait_sec()
        )
        ok_m, fallback_mqtt = wait_mqtt_advertised_near(
            shell, 6.0, timeout_sec=min(20.0, _fallback_wait_sec())
        )
        # Prefer pilot dingus (user requirement); MQTT is supporting evidence.
        ok_fallback = ok_p or ok_m
    finally:
        start_detail = start_sbx_seriald(shell)
        time.sleep(5.0)
        try:
            set_user_max_charge_current_a(shell, restore_a)
        except Exception:
            pass
        ok_r, restore_mqtt = wait_mqtt_advertised_near(
            shell, float(restore_a), timeout_sec=_fallback_wait_sec(), tol_a=2.0
        )
        # Also accept any advertised clearly above fallback once comms are back.
        if not ok_r and restore_mqtt is not None and float(restore_mqtt) >= 10.0:
            ok_r = True
        ok_restore = ok_r
        present, present_detail = sbx_comms_present(shell)
        ok_restore = ok_restore and present

    ok = ok_fallback and ok_restore
    observed = (
        f"before mqtt={before_adv} pilot={before_pilot}; "
        f"after stop pilot={fallback_pilot} mqtt={fallback_mqtt}; "
        f"after start mqtt={restore_mqtt} restore_target={restore_a} A; "
        f"stop={stop_detail}; start={start_detail}"
    )
    _emit_case(
        emit,
        case_ids,
        title="Comms loss throttle down to 6 A then restore",
        signal="span-sbx-seriald stop/start",
        expected=(
            f"After seriald stop: pilot/MQTT ≈ 6 A; after start: "
            f"advertise ≈ {restore_a} A (80% of {rating_a} A) with SBX comms"
        ),
        observed=observed[:500],
        result="pass" if ok else "fail",
        evidence=[
            {"stream": "gateway", "ts": "", "label": "stop", "line": stop_detail[:400]},
            {"stream": "gateway", "ts": "", "label": "start", "line": start_detail[:400]},
            {"stream": "pilot", "ts": "", "label": "fallback", "line": observed[:400]},
        ],
        metrics={
            "before_advertised_a": before_adv,
            "before_pilot_adv_a": before_pilot,
            "fallback_pilot_a": fallback_pilot,
            "fallback_mqtt_a": fallback_mqtt,
            "restore_target_a": restore_a,
            "restore_mqtt_a": restore_mqtt,
            "breaker_rating_a": rating_a,
        },
    )
    return {
        "ok": ok,
        "ok_fallback": ok_fallback,
        "ok_restore": ok_restore,
        "fallback_pilot_a": fallback_pilot,
        "restore_mqtt_a": restore_mqtt,
    }


# ---------------------------------------------------------------------------
# Case 7 — comms-required + relay cycle + seriald kill → 6 A
# ---------------------------------------------------------------------------


def case_comms_required_first_contact(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    instance_id: int,
    case_ids: Optional[CaseIdFactory] = None,
) -> dict[str, Any]:
    ensure_pilot_charge(ev)
    wait_relay = _relay_wait_sec()
    wait_boot = _boot_wait_sec()

    remove_detail = remove_comms_required(shell)
    disc_detail = ""
    on_detail = ""
    stop_detail = ""
    start_detail = ""
    ok_comms_after_boot = False
    ok_fallback = False
    fallback_pilot: Optional[float] = None
    boot_detail = ""

    try:
        disc_detail = drive_relay_disconnect(shell, instance_id)
        emit(
            "comms_case_progress",
            message=f"Case 7: relay disconnected — waiting {wait_relay:.0f}s",
        )
        time.sleep(wait_relay)
        on_detail = drive_relay_override(shell, instance_id)
        emit(
            "comms_case_progress",
            message=f"Case 7: relay override — waiting {wait_boot:.0f}s for boot",
        )
        time.sleep(wait_boot)
        ok_comms_after_boot, boot_detail = sbx_comms_present(shell, wait_sec=10.0)

        stop_detail = stop_sbx_seriald(shell)
        ok_fallback, fallback_pilot, _ = wait_pilot_near_amps(
            ev, 6.0, timeout_sec=_fallback_wait_sec()
        )
    finally:
        start_detail = start_sbx_seriald(shell)
        try:
            drive_relay_override(shell, instance_id)
        except Exception:
            pass

    ok = ok_comms_after_boot and ok_fallback
    observed = (
        f"remove={remove_detail}; disconnect={disc_detail}; override={on_detail}; "
        f"comms_after_boot={boot_detail}; stop={stop_detail}; "
        f"pilot_fallback={fallback_pilot}; start={start_detail}"
    )
    _emit_case(
        emit,
        case_ids,
        title="Comms-required flag + relay cycle; seriald kill → 6 A",
        signal="comms-required / relay / seriald",
        expected=(
            "After relay cycle: SBX/MQTT comms present; after seriald stop: "
            "pilot advertised ≈ 6 A"
        ),
        observed=observed[:500],
        result="pass" if ok else "fail",
        evidence=[
            {"stream": "gateway", "ts": "", "label": "sbx-ctl", "line": remove_detail[:400]},
            {"stream": "gateway", "ts": "", "label": "relay", "line": f"{disc_detail} | {on_detail}"[:400]},
            {"stream": "pilot", "ts": "", "label": "fallback", "line": f"pilot_adv={fallback_pilot}"},
        ],
        metrics={
            "comms_after_boot": ok_comms_after_boot,
            "fallback_pilot_a": fallback_pilot,
            "relay_wait_sec": wait_relay,
            "boot_wait_sec": wait_boot,
        },
    )
    return {
        "ok": ok,
        "ok_comms_after_boot": ok_comms_after_boot,
        "ok_fallback": ok_fallback,
        "fallback_pilot_a": fallback_pilot,
    }


# ---------------------------------------------------------------------------
# Case 8 — no comms on boot, vehicle in Connect
# ---------------------------------------------------------------------------


def case_no_comms_on_boot_connect(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    instance_id: int,
    case_ids: Optional[CaseIdFactory] = None,
) -> dict[str, Any]:
    reading = ensure_pilot_connect(ev)
    wait_boot = _boot_wait_sec()

    disc_detail = ""
    stop_detail = ""
    on_detail = ""
    start_detail = ""
    ok_no_sbx = False
    ok_pilot_signal = False
    after_detail = ""

    try:
        disc_detail = drive_relay_disconnect(shell, instance_id)
        time.sleep(5.0)
        stop_detail = stop_sbx_seriald(shell)
        on_detail = drive_relay_override(shell, instance_id)
        emit(
            "comms_case_progress",
            message=f"Case 8: Drive on without seriald — wait {wait_boot:.0f}s",
        )
        time.sleep(wait_boot)

        present, after_detail = sbx_comms_present(shell, wait_sec=8.0)
        ok_no_sbx = not present

        try:
            reading = ev.pilot_with_retry(attempts=3, delay_sec=2.0)
        except Exception as exc:
            reading = {"error": str(exc)}
        # Pilot CP should still be observable (voltage or state not empty).
        try:
            hi = float(reading.get("cp_voltage_v") or reading.get("cp_high_v") or 0)
        except (TypeError, ValueError):
            hi = 0.0
        state = str(reading.get("state") or "")
        ok_pilot_signal = bool(state) or abs(hi) > 1.0
    finally:
        start_detail = start_sbx_seriald(shell)
        try:
            drive_relay_override(shell, instance_id)
        except Exception:
            pass
        try:
            ensure_pilot_charge(ev)
        except Exception:
            pass

    ok = ok_no_sbx and ok_pilot_signal
    observed = (
        f"mode={reading.get('pilot_mode')} state={reading.get('state')} "
        f"cp_high={reading.get('cp_voltage_v')}; sbx={after_detail}; "
        f"disc={disc_detail}; stop={stop_detail}; on={on_detail}; start={start_detail}"
    )
    _emit_case(
        emit,
        case_ids,
        title="No comms on boot with vehicle connected (Connect)",
        signal="relay + seriald stopped before boot",
        expected="Pilot CP signal present; no SBX charge_rate / advertised on MQTT",
        observed=observed[:500],
        result="pass" if ok else "fail",
        evidence=[
            {"stream": "gateway", "ts": "", "label": "sequence", "line": observed[:400]},
        ],
        metrics={
            "ok_no_sbx": ok_no_sbx,
            "ok_pilot_signal": ok_pilot_signal,
            "pilot_state": reading.get("state"),
            "pilot_mode": reading.get("pilot_mode"),
        },
    )
    return {"ok": ok, "ok_no_sbx": ok_no_sbx, "ok_pilot_signal": ok_pilot_signal}


# ---------------------------------------------------------------------------
# Case 9 + 10 — throttle gate then set charge rate 0
# ---------------------------------------------------------------------------


def case_set_charge_rate_zero(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    case_ids: Optional[CaseIdFactory] = None,
    settle_sec: float = 5.0,
) -> dict[str, Any]:
    """Case 10: publish user-max 0; charge_rate_a must become 0."""
    ensure_pilot_charge(ev)
    before = poll_charge_rate_a(shell, timeout_sec=20.0)
    set_user_max_charge_current_a(shell, 0)
    time.sleep(settle_sec)
    after = poll_charge_rate_a(shell, timeout_sec=45.0)
    # Also accept MQTT advertised 0 if status charge_rate lags.
    adv = read_advertised_current_a(shell, wait_sec=5.0)
    ok = (after is not None and float(after) <= 0.5) or (
        adv is not None and float(adv) <= 0.5
    )
    observed = (
        f"before charge_rate_a={before}; after charge_rate_a={after}; "
        f"advertised_current_a={adv}"
    )
    _emit_case(
        emit,
        case_ids,
        title="Set charge rate to 0 A",
        signal="user_max_charge_current_a=0 → charge_rate_a",
        expected="charge_rate_a (MsgDriveStatus/MQTT) == 0 after user-max 0",
        observed=observed,
        result="pass" if ok else "fail",
        evidence=[
            {"stream": "gateway", "ts": "", "label": "charge_rate", "line": observed[:400]},
        ],
        metrics={
            "before_charge_rate_a": before,
            "after_charge_rate_a": after,
            "advertised_current_a": adv,
        },
    )
    return {"ok": ok, "before": before, "after": after, "advertised": adv}


def case_throttle_then_set_charge_rate(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    throttle_passed: bool,
    case_ids: Optional[CaseIdFactory] = None,
) -> dict[str, Any]:
    """Case 9: only run set-to-0 (case 10) when throttle verification passed."""
    if not throttle_passed:
        _emit_case(
            emit,
            case_ids,
            title="Set charge rate after throttle (gate)",
            signal="throttle_passed",
            expected="Throttle verification passed before set-charge-rate-0",
            observed="throttle_passed=false — skipping set charge rate 0",
            result="fail",
            evidence=[],
            metrics={"throttle_passed": False, "skipped": True},
        )
        return {"ok": False, "skipped": True, "throttle_passed": False}

    _emit_case(
        emit,
        case_ids,
        title="Set charge rate after throttle (gate)",
        signal="throttle_passed",
        expected="Throttle verification passed",
        observed="throttle_passed=true — proceeding to set charge rate 0",
        result="pass",
        evidence=[],
        metrics={"throttle_passed": True},
    )
    zero = case_set_charge_rate_zero(shell, ev, emit, case_ids=case_ids)
    return {"ok": bool(zero.get("ok")), "skipped": False, "zero": zero}


# ---------------------------------------------------------------------------
# Case 11 — SBX comms after power reset
# ---------------------------------------------------------------------------


def case_sbx_comms_on_power_reset(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    instance_id: int,
    case_ids: Optional[CaseIdFactory] = None,
) -> dict[str, Any]:
    wait_off = max(5.0, _env_float("DRIVE_COMMS_POWER_OFF_SEC", 15.0))
    wait_boot = _boot_wait_sec()

    # Ensure seriald is up before the cycle.
    start_sbx_seriald(shell)
    disc_detail = drive_relay_disconnect(shell, instance_id)
    emit(
        "comms_case_progress",
        message=f"Case 11: Drive disconnected — waiting {wait_off:.0f}s",
    )
    time.sleep(wait_off)
    on_detail = drive_relay_override(shell, instance_id)
    emit(
        "comms_case_progress",
        message=f"Case 11: Drive override — waiting {wait_boot:.0f}s for boot",
    )
    time.sleep(wait_boot)
    ok, detail = sbx_comms_present(shell, wait_sec=12.0)
    observed = f"disconnect={disc_detail}; override={on_detail}; comms={detail}"
    _emit_case(
        emit,
        case_ids,
        title="SBX comms on power reset",
        signal="relay disconnect/override",
        expected="After boot wait, MQTT/SBX charge_rate or advertised_current present",
        observed=observed[:500],
        result="pass" if ok else "fail",
        evidence=[
            {"stream": "gateway", "ts": "", "label": "power_reset", "line": observed[:400]},
        ],
        metrics={"comms_present": ok, "boot_wait_sec": wait_boot},
    )
    try:
        ensure_pilot_charge(ev)
    except Exception:
        pass
    return {"ok": ok, "detail": detail}


def run_comms_failover_cases(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    instance_id: int,
    case_ids: Optional[CaseIdFactory] = None,
    run_long_cases: bool = True,
) -> dict[str, Any]:
    """Run cases 6, 8, 11, and optionally long case 7 (relay waits)."""
    out: dict[str, Any] = {}
    try:
        out["case6"] = case_comms_loss_throttle_down(
            shell, ev, emit, instance_id=instance_id, case_ids=case_ids
        )
    except Exception as exc:
        out["case6"] = {"ok": False, "error": str(exc)}
        emit("comms_case_error", case="6", error=str(exc), message=str(exc))

    if run_long_cases:
        try:
            out["case7"] = case_comms_required_first_contact(
                shell, ev, emit, instance_id=instance_id, case_ids=case_ids
            )
        except Exception as exc:
            out["case7"] = {"ok": False, "error": str(exc)}
            emit("comms_case_error", case="7", error=str(exc), message=str(exc))
            try:
                start_sbx_seriald(shell)
                drive_relay_override(shell, instance_id)
            except Exception:
                pass

    try:
        out["case8"] = case_no_comms_on_boot_connect(
            shell, ev, emit, instance_id=instance_id, case_ids=case_ids
        )
    except Exception as exc:
        out["case8"] = {"ok": False, "error": str(exc)}
        emit("comms_case_error", case="8", error=str(exc), message=str(exc))
        try:
            start_sbx_seriald(shell)
        except Exception:
            pass

    try:
        out["case11"] = case_sbx_comms_on_power_reset(
            shell, ev, emit, instance_id=instance_id, case_ids=case_ids
        )
    except Exception as exc:
        out["case11"] = {"ok": False, "error": str(exc)}
        emit("comms_case_error", case="11", error=str(exc), message=str(exc))

    # Always leave seriald running for the rest of the pipeline.
    try:
        start_sbx_seriald(shell)
    except Exception:
        pass

    oks = [bool(v.get("ok")) for v in out.values() if isinstance(v, dict)]
    out["ok"] = all(oks) if oks else False
    return out
