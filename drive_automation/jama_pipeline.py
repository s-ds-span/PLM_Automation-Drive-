"""Dedicated JAMA-test pipeline (EV Simulator + gateway only — no LB2).

Flow:
  1. Snapshot mutable gateway/pilot state (user-max, seriald, pilot mode).
  2. Ensure Charge mode; run selected cases from ``case_selection``.
  3. Always restore snapshot in ``finally`` (even when a case/command fails).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from drive_automation.advertise_cases import (
    advertise_integer_rates,
    ensure_pilot_charge,
    obeys_breaker_rating_as_max,
    read_user_max_charge_current_a,
    set_user_max_charge_current_a,
)
from drive_automation.case_selection import (
    CASE_LABELS,
    case_enabled,
    resolve_jama_cases,
)
from drive_automation.comms_cases import (
    case_comms_loss_throttle_down,
    case_comms_required_first_contact,
    case_no_comms_on_boot_connect,
    case_sbx_comms_on_power_reset,
    case_set_charge_rate_zero,
    drive_relay_override,
    start_sbx_seriald,
    stop_sbx_seriald,
)
from drive_automation.gateway import GatewayShell
from drive_automation.jama_cases import CaseIdFactory, emit_jama_case

EmitFn = Callable[..., None]

JAMA_PIPELINE_PHASES = (
    "capture",
    "boot",
    "handshake",
    "snapshot",
    "jama_test",
    "restore",
)


@dataclass
class JamaSnapshot:
    """Values we may mutate — restored on success or failure."""

    user_max_a: Optional[float] = None
    seriald_active: bool = True
    pilot_mode: str = "watch"
    breaker_instance_id: Optional[int] = None
    extras: dict[str, Any] = field(default_factory=dict)


def _run(shell: GatewayShell, command: str, *, timeout_s: float = 45) -> tuple[int, str]:
    code, out, err = shell.run(command, timeout_s=timeout_s)
    return code, ((out or err or "").strip())


def snapshot_jama_state(
    shell: GatewayShell,
    ev: Any,
    *,
    breaker_instance_id: Optional[int] = None,
) -> JamaSnapshot:
    user_max: Optional[float] = None
    try:
        user_max = read_user_max_charge_current_a(shell)
    except Exception:
        user_max = None

    code, text = _run(shell, "systemctl is-active span-sbx-seriald", timeout_s=20)
    seriald_active = code == 0 and "active" in (text or "").lower()

    pilot_mode = "watch"
    try:
        reading = ev.pilot(timeout=8.0)
        pilot_mode = str(reading.get("pilot_mode") or "watch").strip().lower()
    except Exception:
        pass

    return JamaSnapshot(
        user_max_a=user_max,
        seriald_active=seriald_active,
        pilot_mode=pilot_mode or "watch",
        breaker_instance_id=breaker_instance_id,
    )


def restore_jama_state(
    shell: GatewayShell,
    ev: Any,
    snap: JamaSnapshot,
    emit: EmitFn,
) -> None:
    """Best-effort revert to pre-pipeline conditions."""
    steps: list[str] = []

    # 1) SBX seriald must be running for normal lab operation.
    try:
        if snap.seriald_active:
            detail = start_sbx_seriald(shell)
            steps.append(f"seriald start: {detail}")
        else:
            # Unusual, but honor snapshot.
            detail = stop_sbx_seriald(shell)
            steps.append(f"seriald stop (was inactive): {detail}")
    except Exception as exc:
        steps.append(f"seriald restore error: {exc}")

    # 2) Drive relay override (undo disconnect from power-reset / comms cases).
    if snap.breaker_instance_id is not None:
        try:
            detail = drive_relay_override(shell, int(snap.breaker_instance_id))
            steps.append(f"relay override: {detail}")
        except Exception as exc:
            steps.append(f"relay restore error: {exc}")

    # 3) user-max charge current.
    if snap.user_max_a is not None:
        try:
            amps = int(round(float(snap.user_max_a)))
            set_user_max_charge_current_a(shell, amps)
            time.sleep(1.0)
            steps.append(f"user_max restored to {amps} A")
        except Exception as exc:
            steps.append(f"user_max restore error: {exc}")
    else:
        steps.append("user_max unknown at start — left unchanged")

    # 4) Pilot mode.
    mode = str(snap.pilot_mode or "watch").lower()
    if mode not in ("watch", "connect", "charge", "vent"):
        mode = "watch"
    try:
        if mode in ("connect", "watch", "vent"):
            try:
                ev.ensure_loads_off()
            except Exception:
                pass
        ev.set_mode_with_retry(mode, attempts=3, delay_sec=5.0)
        steps.append(f"pilot mode → {mode}")
    except Exception as exc:
        steps.append(f"pilot restore error: {exc}")

    emit(
        "jama_restore_done",
        snapshot={
            "user_max_a": snap.user_max_a,
            "seriald_active": snap.seriald_active,
            "pilot_mode": snap.pilot_mode,
            "breaker_instance_id": snap.breaker_instance_id,
        },
        steps=steps,
        message="JAMA pipeline restore: " + "; ".join(steps[:6]),
    )


def run_selected_jama_cases(
    shell: GatewayShell,
    ev: Any,
    emit: EmitFn,
    *,
    instance_id: int,
    case_ids: CaseIdFactory,
    selected: Optional[frozenset[str]] = None,
) -> dict[str, Any]:
    """Run only enabled cases; each failure is recorded, then continue."""
    cases = resolve_jama_cases(selected)
    emit(
        "jama_cases_selected",
        cases=sorted(cases),
        labels={c: CASE_LABELS.get(c, c) for c in sorted(cases)},
        message="JAMA cases: " + ", ".join(sorted(cases)),
    )
    out: dict[str, Any] = {"selected": sorted(cases)}

    ensure_pilot_charge(ev)

    if case_enabled(cases, "advertise_rates"):
        try:
            out["advertise_rates"] = advertise_integer_rates(
                shell, ev, emit, instance_id=instance_id, case_ids=case_ids
            )
        except Exception as exc:
            out["advertise_rates"] = {"ok": False, "error": str(exc)}
            emit("jama_case_error", case="advertise_rates", error=str(exc), message=str(exc))

    if case_enabled(cases, "obeys_breaker"):
        try:
            out["obeys_breaker"] = obeys_breaker_rating_as_max(
                shell, ev, emit, instance_id=instance_id, case_ids=case_ids
            )
        except Exception as exc:
            out["obeys_breaker"] = {"ok": False, "error": str(exc)}
            emit("jama_case_error", case="obeys_breaker", error=str(exc), message=str(exc))

    if case_enabled(cases, "comms_loss"):
        try:
            out["comms_loss"] = case_comms_loss_throttle_down(
                shell, ev, emit, instance_id=instance_id, case_ids=case_ids
            )
        except Exception as exc:
            out["comms_loss"] = {"ok": False, "error": str(exc)}
            emit("jama_case_error", case="comms_loss", error=str(exc), message=str(exc))

    if case_enabled(cases, "comms_required"):
        try:
            out["comms_required"] = case_comms_required_first_contact(
                shell, ev, emit, instance_id=instance_id, case_ids=case_ids
            )
        except Exception as exc:
            out["comms_required"] = {"ok": False, "error": str(exc)}
            emit("jama_case_error", case="comms_required", error=str(exc), message=str(exc))

    if case_enabled(cases, "no_comms_boot"):
        try:
            out["no_comms_boot"] = case_no_comms_on_boot_connect(
                shell, ev, emit, instance_id=instance_id, case_ids=case_ids
            )
        except Exception as exc:
            out["no_comms_boot"] = {"ok": False, "error": str(exc)}
            emit("jama_case_error", case="no_comms_boot", error=str(exc), message=str(exc))

    if case_enabled(cases, "set_charge_rate_0"):
        try:
            # JAMA pipeline: no throttle gate — run set-to-0 directly.
            out["set_charge_rate_0"] = case_set_charge_rate_zero(
                shell, ev, emit, case_ids=case_ids
            )
        except Exception as exc:
            out["set_charge_rate_0"] = {"ok": False, "error": str(exc)}
            emit(
                "jama_case_error",
                case="set_charge_rate_0",
                error=str(exc),
                message=str(exc),
            )

    if case_enabled(cases, "power_reset"):
        try:
            out["power_reset"] = case_sbx_comms_on_power_reset(
                shell, ev, emit, instance_id=instance_id, case_ids=case_ids
            )
        except Exception as exc:
            out["power_reset"] = {"ok": False, "error": str(exc)}
            emit("jama_case_error", case="power_reset", error=str(exc), message=str(exc))

    oks = [
        bool(v.get("ok"))
        for k, v in out.items()
        if k != "selected" and isinstance(v, dict) and "ok" in v
    ]
    out["ok"] = all(oks) if oks else False
    return out
