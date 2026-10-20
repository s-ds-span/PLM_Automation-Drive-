"""Closed-loop LB2 load control — panel throttle setpoint > pilot advertised current."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from drive_automation.clients import Lb2Client
from drive_automation.jama_cases import CaseIdFactory, emit_jama_case
from drive_automation.load_selection import (
    loads_to_amps,
    max_panel_amps,
    select_loads_panel_total,
)

EmitFn = Callable[..., None]
StopCheck = Callable[[], bool]


@dataclass


class LoadControllerParams:
    breaker_rating_a: float = 60.0
    max_lb2_amps: float = 40.0
    loop_interval_sec: float = 1.0
    deadband_a: float = 0.75
    slew_a_per_s: float = 13.0
    panel_channels: tuple[int, ...] = (1, 2, 3, 4)
    channel_voltages: Optional[dict[int, str]] = None
    aggregation_timeout_sec: float = 180.0
    sustain_timeout_sec: float = 90.0
    recovery_timeout_sec: float = 60.0
    throttle_drop_ratio: float = 0.75
    aggregation_step_a: float = 5.0
    # Pilot duty ≤ this % means Drive/CP is fully throttled (no ceiling raise prompt).
    full_throttle_duty_pct: float = 10.0
    # Extra settle after LB2 step so Modbus + feeder + meter are aligned before JAMA.
    metrics_settle_sec: float = 2.5
    # Wait for MsgDriveStatus.observed_charge_rate_a after each step sample.
    observed_wait_sec: float = 8.0
    # |panel_total − (LB2_meter/fallback + observed_drive)| tolerance for balance PASS.
    balance_tolerance_a: float = 3.0
    # After charge_rate drops, wait for pilot duty to track before next LB2 step
    # (EV load follows duty — stepping LB2 while duty is stale can overshoot/fault).
    duty_follow_timeout_sec: float = 15.0
    duty_follow_tolerance_a: float = 3.0
    duty_follow_poll_sec: float = 0.5


def _duty_implied_amps(duty_pct: Optional[float]) -> Optional[float]:
    """IEC 61851 continuous range: Imax ≈ duty% × 0.6 (10%–85% duty)."""
    if duty_pct is None:
        return None
    try:
        duty = float(duty_pct)
    except (TypeError, ValueError):
        return None
    if 10.0 <= duty <= 85.0:
        return round(duty * 0.6, 2)
    return None


def _duty_tracks_charge_rate(
    *,
    charge_rate_a: Optional[float],
    duty_pct: Optional[float],
    full_throttle_duty_pct: float,
    tolerance_a: float,
) -> bool:
    """True when pilot duty has caught up with a reduced panel charge_rate."""
    if charge_rate_a is None or duty_pct is None:
        return False
    cr = float(charge_rate_a)
    duty = float(duty_pct)
    if cr <= 6.0 and duty <= float(full_throttle_duty_pct):
        return True
    implied = _duty_implied_amps(duty)
    if implied is None:
        # Outside continuous PWM band — treat low duty as tracked when CR is low.
        return cr <= 6.0 and duty <= float(full_throttle_duty_pct)
    return implied <= cr + float(tolerance_a)


def _observed_amps_from_detail(detail: Optional[dict[str, Any]]) -> Optional[float]:
    if not detail:
        return None
    raw = detail.get("observed_amps")
    if raw is None and detail.get("evidence"):
        import re

        m = re.search(r"observed_charge_rate_a=(\d+)", str(detail["evidence"]), re.I)
        if m:
            raw = m.group(1)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _detail_is_drive_status_with_observed(
    detail: Optional[dict[str, Any]],
) -> bool:
    """True when detail is MsgDriveStatus carrying observed_charge_rate_a."""
    if detail is None or detail.get("observed_amps") is None:
        return False
    src = str(detail.get("source") or "")
    evid = str(detail.get("evidence") or "")
    return (
        "DriveStatus" in src
        or "MsgDriveStatus" in evid
        or "observed_charge_rate_a=" in evid.lower()
    )


def _fault_info_from_detail(detail: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return active fault info from a charge-rate detail row, if any.

    Requires the evidence line itself to carry ``fault_mask`` / FAULT state so a
    stale mask cannot be paired with a ``MsgSetChargeRate`` evidence snippet.
    """
    if not detail:
        return None
    evid = str(detail.get("evidence") or "")
    import re

    mask = str(detail.get("fault_mask") or "").lower()
    state = str(detail.get("charging_state") or "")
    if evid:
        m = re.search(r"fault_mask=(0x[0-9A-Fa-f]+)", evid, re.I)
        if m:
            mask = m.group(1).lower()
        elif "fault_mask=" not in evid.lower():
            mask = ""
        m2 = re.search(r"charging_state=([A-Z0-9_]+)", evid, re.I)
        if m2:
            state = m2.group(1)
        elif "charging_state=" not in evid.lower():
            state = ""
    active_mask = bool(mask) and mask not in ("0x00000000", "0x0", "none", "")
    active_state = "FAULT" in state.upper()
    if not active_mask and not active_state:
        return None
    return {
        "fault_mask": mask if active_mask else None,
        "charging_state": state if state else None,
        "evidence": detail.get("evidence"),
        "ts": detail.get("ts") or "",
        "source": detail.get("source") or "MsgDriveStatus",
    }


def _observed_metric_value(observed_a: Optional[float]) -> Any:
    """Report metric: numeric amps, or explicit 'not observed'."""
    if observed_a is None:
        return "not observed"
    return observed_a


def _observed_text(observed_a: Optional[float]) -> str:
    if observed_a is None:
        return " · observed not observed"
    return f" · observed {observed_a:g} A"


def _emit_sbx_fault_step_case(
    emit: EmitFn,
    *,
    ids: CaseIdFactory,
    section: str,
    title: str,
    fault: dict[str, Any],
    iteration: int,
    commanded_amps: float,
) -> None:
    """JAMA case highlighting an active Drive fault at this throttle step."""
    mask = fault.get("fault_mask") or "—"
    state = fault.get("charging_state") or "—"
    evidence: list[dict[str, Any]] = []
    if fault.get("evidence"):
        evidence.append(
            {
                "stream": "modbus",
                "ts": fault.get("ts") or "",
                "line": str(fault.get("evidence"))[:400],
                "label": "sbx_fault",
            }
        )
    emit_jama_case(
        emit,
        case_id=ids.next("SBX"),
        section=section,
        title=title,
        signal="fault_mask / charging_state",
        expected="fault_mask=0x0 and charging_state not FAULT",
        observed=(
            f"fault_mask={mask} · charging_state={state} "
            f"at LB2 {commanded_amps:g} A — charge_rate/balance may be invalid"
        ),
        sources="modbus",
        result="fail",
        evidence=evidence,
        show_evidence=True,
        metrics={
            "fault_mask": mask,
            "charging_state": state,
            "lb2_load_a": round(float(commanded_amps), 2),
        },
        iteration=iteration,
    )


def _resolve_lb2_for_balance(
    *,
    meter_a: Optional[float] = None,
    achieved_a: Optional[float] = None,
    commanded_a: float = 0.0,
) -> tuple[float, str]:
    """Prefer actual applied current for panel ≈ LB2 + drive checks.

    Order: LB2 meter (API) → relay-model achieved → commanded setpoint.
    """
    if meter_a is not None and float(meter_a) > 0:
        return float(meter_a), "meter"
    if achieved_a is not None and float(achieved_a) > 0:
        return float(achieved_a), "achieved"
    return float(commanded_a), "commanded"


def _debug_bal_log(
    *,
    hypothesis_id: str,
    location: str,
    message: str,
    data: dict[str, Any],
) -> None:
    # #region agent log
    try:
        import json
        from pathlib import Path

        payload = {
            "sessionId": "76aad4",
            "runId": "bal-meter",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        log_path = Path(
            "/Users/suhas.ds/project/drive_automation/.cursor/debug-76aad4.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass
    # #endregion


def _emit_current_balance_case(
    emit: EmitFn,
    *,
    ids: CaseIdFactory,
    section: str,
    title: str,
    panel_total: Optional[float],
    lb2_commanded_a: float,
    observed_a: Optional[float],
    request_a: Optional[float],
    tolerance_a: float,
    evidence: list[dict[str, Any]],
    iteration: int,
    lb2_meter_a: Optional[float] = None,
    lb2_achieved_a: Optional[float] = None,
) -> dict[str, Any]:
    """JAMA case: panel_total ≈ LB2(actual) + observed_charge_rate (fallback: request)."""
    lb2_a, lb2_source = _resolve_lb2_for_balance(
        meter_a=lb2_meter_a,
        achieved_a=lb2_achieved_a,
        commanded_a=lb2_commanded_a,
    )
    drive_a = observed_a if observed_a is not None else request_a
    drive_label = "observed" if observed_a is not None else "request"
    lb2_label = f"LB2 {lb2_source}"
    metrics: dict[str, Any] = {
        "sbx_request_a": request_a,
        "observed_charge_rate_a": _observed_metric_value(observed_a),
        "panel_total_a": panel_total,
        "lb2_load_a": round(float(lb2_commanded_a), 2),
        "lb2_meter_a": (
            round(float(lb2_meter_a), 2) if lb2_meter_a is not None else None
        ),
        "lb2_achieved_a": (
            round(float(lb2_achieved_a), 2) if lb2_achieved_a is not None else None
        ),
        "lb2_balance_source": lb2_source,
        "lb2_balance_a": round(float(lb2_a), 2),
    }
    # #region agent log
    _debug_bal_log(
        hypothesis_id="H1",
        location="load_controller.py:_emit_current_balance_case",
        message="balance lb2 source resolved",
        data={
            "iteration": iteration,
            "title": title,
            "section": section,
            "panel_total": panel_total,
            "lb2_commanded_a": lb2_commanded_a,
            "lb2_meter_a": lb2_meter_a,
            "lb2_achieved_a": lb2_achieved_a,
            "lb2_balance_a": lb2_a,
            "lb2_source": lb2_source,
            "observed_a": observed_a,
            "request_a": request_a,
            "drive_a": drive_a,
            "drive_label": drive_label,
        },
    )
    # #endregion
    if panel_total is None or drive_a is None:
        ok = False
        expected = (
            f"panel_total ≈ {lb2_label} + {drive_label} drive "
            f"(tolerance ±{tolerance_a:g} A)"
        )
        observed = (
            f"panel={panel_total if panel_total is not None else '—'} A · "
            f"{lb2_label}={lb2_a:g} A · "
            f"{drive_label}={drive_a if drive_a is not None else '—'} A "
            "(incomplete sample)"
        )
        metrics["balance_ok"] = False
    else:
        expected_sum = float(lb2_a) + float(drive_a)
        err = abs(float(panel_total) - expected_sum)
        ok = err <= float(tolerance_a)
        metrics["lb2_plus_drive_a"] = round(expected_sum, 2)
        metrics["balance_error_a"] = round(err, 2)
        metrics["balance_ok"] = ok
        expected = (
            f"panel_total ≈ {lb2_label} + {drive_label} "
            f"({lb2_a:g}+{drive_a:g}={expected_sum:g} A ±{tolerance_a:g} A)"
        )
        observed = (
            f"panel {panel_total:g} A vs {lb2_label} {lb2_a:g} + "
            f"{drive_label} {drive_a:g} = {expected_sum:g} A (Δ {err:g} A)"
        )
    # #region agent log
    _debug_bal_log(
        hypothesis_id="H1",
        location="load_controller.py:_emit_current_balance_case:result",
        message="balance case result",
        data={
            "iteration": iteration,
            "title": title,
            "ok": ok,
            "lb2_source": lb2_source,
            "balance_error_a": metrics.get("balance_error_a"),
            "lb2_plus_drive_a": metrics.get("lb2_plus_drive_a"),
        },
    )
    # #endregion
    emit_jama_case(
        emit,
        case_id=ids.next("BAL"),
        section=section,
        title=title,
        signal=f"panel_total ≈ LB2({lb2_source}) + observed_charge_rate",
        expected=expected,
        observed=observed,
        sources="modbus, gateway, lb2",
        result="pass" if ok else "fail",
        evidence=evidence,
        show_evidence=True,
        metrics=metrics,
        iteration=iteration,
    )
    return metrics


@dataclass


class SetpointReading:
    amps: Optional[float]
    source: str  # panel | pilot | none
    panel_charge_rate_a: Optional[float] = None
    pilot_adv_current_a: Optional[float] = None
    duty_cycle_pct: Optional[float] = None


class LoadController:
    """
    Dynamic LB2 commander.

    Priority: panel charge_rate_a (throttled CP current) > pilot adv_current_a.
    Uses existing Lb2Client.apply_channel_loads only — no new LB access path.
    """

    def __init__(
        self,
        lb2: Lb2Client,
        params: LoadControllerParams,
        *,
        emit: EmitFn,
        stop_check: Optional[StopCheck] = None,
    ) -> None:
        self.lb2 = lb2
        self.params = params
        self._emit = emit
        self._stop_check = stop_check or (lambda: False)
        self.commanded_amps = 0.0
        self.baseline_pilot_adv: Optional[float] = None
        self.baseline_duty: Optional[float] = None
        self.baseline_panel_rate: Optional[float] = None

    def resolve_setpoint(
        self,
        *,
        panel_charge_rate_a: Optional[float],
        pilot_adv_current_a: Optional[float],
        duty_cycle_pct: Optional[float] = None,
    ) -> SetpointReading:
        if panel_charge_rate_a is not None and panel_charge_rate_a >= 0:
            return SetpointReading(
                amps=float(panel_charge_rate_a),
                source="panel",
                panel_charge_rate_a=panel_charge_rate_a,
                pilot_adv_current_a=pilot_adv_current_a,
                duty_cycle_pct=duty_cycle_pct,
            )
        if pilot_adv_current_a is not None and pilot_adv_current_a >= 0:
            return SetpointReading(
                amps=float(pilot_adv_current_a),
                source="pilot",
                panel_charge_rate_a=panel_charge_rate_a,
                pilot_adv_current_a=pilot_adv_current_a,
                duty_cycle_pct=duty_cycle_pct,
            )
        return SetpointReading(
            amps=None,
            source="none",
            panel_charge_rate_a=panel_charge_rate_a,
            pilot_adv_current_a=pilot_adv_current_a,
            duty_cycle_pct=duty_cycle_pct,
        )

    def apply_commanded_amps(self, target: float, *, iteration: int = 0) -> float:
        """Slew commanded amps toward target and apply discrete loads."""
        ceiling = max(0.0, float(self.params.max_lb2_amps))
        target = max(0.0, min(float(target), ceiling))
        interval = max(0.2, float(self.params.loop_interval_sec))
        step = max(0.1, float(self.params.slew_a_per_s) * interval)
        current = float(self.commanded_amps)
        if abs(current - target) <= float(self.params.deadband_a):
            current = target
        elif target > current:
            current = min(current + step, target)
        else:
            current = max(current - step, target)

        self.commanded_amps = current
        desired = select_loads_panel_total(
            current,
            self.params.panel_channels,
            channel_voltages=self.params.channel_voltages,
        )
        self.lb2.apply_panel_loads(
            desired, panel_channels=self.params.panel_channels
        )
        if current <= 0.01:
            self.lb2.apply_panel_loads(
                {ch: set() for ch in self.params.panel_channels},
                panel_channels=self.params.panel_channels,
            )

        self._emit(
            "load_adjust",
            iteration=iteration,
            commanded_amps=round(current, 2),
            target_amps=round(target, 2),
            loads={str(ch): sorted(v) for ch, v in desired.items()},
            message=f"LB2 commanded {current:.1f} A (target {target:.1f} A)",
        )
        return current

    def record_baselines(
        self,
        *,
        pilot_adv_current_a: Optional[float],
        duty_cycle_pct: Optional[float],
        panel_charge_rate_a: Optional[float],
    ) -> None:
        self.baseline_pilot_adv = pilot_adv_current_a
        self.baseline_duty = duty_cycle_pct
        self.baseline_panel_rate = panel_charge_rate_a
        self._emit(
            "load_baselines",
            pilot_adv_current_a=pilot_adv_current_a,
            duty_cycle_pct=duty_cycle_pct,
            panel_charge_rate_a=panel_charge_rate_a,
            message="Recorded pre-throttle baselines",
        )

    def throttle_engaged(self, reading: SetpointReading) -> bool:
        """True when panel/pilot current drops vs baseline (throttle likely)."""
        if reading.source == "panel" and reading.amps is not None:
            base = self.baseline_panel_rate or self.baseline_pilot_adv
            if base and base > 0 and reading.amps <= base * self.params.throttle_drop_ratio:
                return True
            if reading.amps <= 10.0 and (base is None or reading.amps < (base or 99)):
                # Typical test target around 8 A once panel has throttled
                if self.baseline_pilot_adv and reading.amps < self.baseline_pilot_adv * 0.9:
                    return True
        if reading.pilot_adv_current_a is not None and self.baseline_pilot_adv:
            if reading.pilot_adv_current_a <= self.baseline_pilot_adv * self.params.throttle_drop_ratio:
                return True
        if reading.duty_cycle_pct is not None and self.baseline_duty:
            if reading.duty_cycle_pct <= self.baseline_duty * self.params.throttle_drop_ratio:
                return True
        return False

    def run_aggregation_and_track(
        self,
        *,
        iteration: int,
        read_setpoint: Callable[[], SetpointReading],
        cp_current_a: Callable[[], Optional[float]],
    ) -> dict[str, Any]:
        """
        Phase 5–7: raise LB2 until over rating or throttle engaged, then track setpoint.
        """
        params = self.params
        first = read_setpoint()
        self.record_baselines(
            pilot_adv_current_a=first.pilot_adv_current_a,
            duty_cycle_pct=first.duty_cycle_pct,
            panel_charge_rate_a=first.panel_charge_rate_a,
        )

        # --- Aggregation ramp ---
        stress = 0.0
        engaged = False
        deadline = time.monotonic() + float(params.aggregation_timeout_sec)
        self._emit(
            "aggregation_start",
            iteration=iteration,
            breaker_rating_a=params.breaker_rating_a,
            message="Raising LB2 until aggregation forces throttle",
        )

        while time.monotonic() < deadline:
            if self._stop_check():
                return {"ok": False, "phase": "aggregation", "reason": "cancelled"}
            reading = read_setpoint()
            self._emit(
                "load_setpoint",
                iteration=iteration,
                setpoint_amps=reading.amps,
                source=reading.source,
                panel_charge_rate_a=reading.panel_charge_rate_a,
                pilot_adv_current_a=reading.pilot_adv_current_a,
                duty_cycle_pct=reading.duty_cycle_pct,
                message=f"Setpoint {reading.amps} A via {reading.source}",
            )
            if self.throttle_engaged(reading):
                engaged = True
                self._emit(
                    "throttle_engaged",
                    iteration=iteration,
                    source=reading.source,
                    setpoint_amps=reading.amps,
                    message="Throttle engaged (panel/pilot drop detected)",
                )
                break

            cp = cp_current_a() or reading.pilot_adv_current_a or 0.0
            meter = self.lb2.meter_snapshot()
            panel_a = float(meter.get("total_amps") or self.commanded_amps or 0.0)
            total = panel_a + float(cp)
            self._emit(
                "aggregation_sample",
                iteration=iteration,
                panel_amps=round(panel_a, 2),
                cp_amps=round(float(cp), 2),
                total_amps=round(total, 2),
                breaker_rating_a=params.breaker_rating_a,
                message=f"Aggregated {total:.1f} A vs rating {params.breaker_rating_a:.0f} A",
            )
            if total > float(params.breaker_rating_a):
                # Keep pushing slightly while waiting for panel to publish throttle
                stress = min(
                    float(params.max_lb2_amps),
                    stress + float(params.aggregation_step_a),
                )
            else:
                stress = min(
                    float(params.max_lb2_amps),
                    max(stress, total - float(cp)) + float(params.aggregation_step_a),
                )
            # Prefer following pilot/panel if present; else stress load
            if reading.amps is not None and reading.source == "panel":
                target = max(stress, float(reading.amps))
            elif reading.amps is not None:
                target = max(stress, float(reading.amps) * 0.5)
            else:
                target = stress
            self.apply_commanded_amps(target, iteration=iteration)
            time.sleep(max(0.2, float(params.loop_interval_sec)))

        if not engaged:
            self._emit(
                "aggregation_timeout",
                iteration=iteration,
                message="Aggregation timed out without clear throttle engagement",
            )

        # --- Sustain / track panel (or pilot) setpoint ---
        sustain_deadline = time.monotonic() + float(params.sustain_timeout_sec)
        verified = False
        self._emit(
            "throttle_sustain_start",
            iteration=iteration,
            message="Tracking throttle setpoint with LB2",
        )
        while time.monotonic() < sustain_deadline:
            if self._stop_check():
                return {
                    "ok": False,
                    "phase": "sustain",
                    "reason": "cancelled",
                    "engaged": engaged,
                }
            reading = read_setpoint()
            self._emit(
                "load_setpoint",
                iteration=iteration,
                setpoint_amps=reading.amps,
                source=reading.source,
                panel_charge_rate_a=reading.panel_charge_rate_a,
                pilot_adv_current_a=reading.pilot_adv_current_a,
                duty_cycle_pct=reading.duty_cycle_pct,
                message=f"Setpoint {reading.amps} A via {reading.source}",
            )
            # Track the CP throttle current with LB2 (panel preferred)
            if reading.amps is not None:
                # External stress should keep throttle active: keep commanded load
                # at least around breaker headroom pressure while following setpoint magnitude.
                follow = float(reading.amps)
                stress_hold = min(
                    float(params.max_lb2_amps),
                    max(self.commanded_amps, float(params.breaker_rating_a) * 0.5),
                )
                target = max(follow, stress_hold * 0.3)
                self.apply_commanded_amps(target, iteration=iteration)

            if self.throttle_engaged(reading):
                duty_ok = True
                if (
                    self.baseline_duty is not None
                    and reading.duty_cycle_pct is not None
                    and reading.duty_cycle_pct > self.baseline_duty * 0.95
                ):
                    duty_ok = False
                if duty_ok:
                    verified = True
                    self._emit(
                        "throttle_verified",
                        iteration=iteration,
                        source=reading.source,
                        setpoint_amps=reading.amps,
                        duty_cycle_pct=reading.duty_cycle_pct,
                        message="Throttle verified against panel/pilot + duty",
                    )
                    break
            time.sleep(max(0.2, float(params.loop_interval_sec)))

        return {
            "ok": engaged or verified,
            "engaged": engaged,
            "verified": verified,
            "commanded_amps": self.commanded_amps,
        }

    def _apply_absolute(self, target: float, *, iteration: int) -> float:
        """Set commanded amps to ``target`` in one discrete step (no slew loop)."""
        soft_ceiling = max(0.0, float(self.params.max_lb2_amps))
        hw_ceiling = max_panel_amps(
            self.params.panel_channels,
            channel_voltages=self.params.channel_voltages,
        )
        # Never command above what the selected channels can actually deliver.
        ceiling = soft_ceiling
        if hw_ceiling > 0:
            ceiling = min(ceiling, hw_ceiling) if soft_ceiling > 0 else hw_ceiling
        target = max(0.0, min(float(target), ceiling if ceiling > 0 else float(target)))
        self.commanded_amps = float(target)
        desired = select_loads_panel_total(
            self.commanded_amps,
            self.params.panel_channels,
            channel_voltages=self.params.channel_voltages,
        )
        self.lb2.apply_panel_loads(
            desired, panel_channels=self.params.panel_channels
        )
        if self.commanded_amps <= 0.01:
            self.lb2.apply_panel_loads(
                {ch: set() for ch in self.params.panel_channels},
                panel_channels=self.params.panel_channels,
            )

        achieved = loads_to_amps(
            desired,
            channel_voltages=self.params.channel_voltages,
        )
        meter_a = None
        try:
            meter_a = float((self.lb2.meter_snapshot() or {}).get("total_amps") or 0.0)
            if meter_a <= 0:
                meter_a = None
        except Exception:
            meter_a = None

        self._emit(
            "load_adjust",
            iteration=iteration,
            commanded_amps=round(self.commanded_amps, 2),
            achieved_amps=achieved,
            meter_amps=meter_a,
            hardware_max_amps=hw_ceiling,
            target_amps=round(target, 2),
            loads={str(ch): sorted(v) for ch, v in desired.items()},
            message=(
                f"LB2 commanded {self.commanded_amps:.1f} A "
                f"(achieved ~{achieved:.1f} A"
                + (f", meter {meter_a:.1f} A" if meter_a is not None else "")
                + f", hw max {hw_ceiling:.1f} A)"
            ),
        )
        return self.commanded_amps

    def _step_to(self, target: float, *, iteration: int) -> None:
        """Drive commanded amps to ``target`` in one discrete step (bounded slew)."""
        ceiling = max(0.0, float(self.params.max_lb2_amps))
        target = max(0.0, min(float(target), ceiling))
        # apply_commanded_amps slews by slew*interval per call; loop until reached.
        guard = 0
        while abs(self.commanded_amps - target) > float(self.params.deadband_a) and guard < 200:
            if self._stop_check():
                return
            self.apply_commanded_amps(target, iteration=iteration)
            guard += 1
            self._sleep(max(0.05, float(self.params.loop_interval_sec)))
        self.apply_commanded_amps(target, iteration=iteration)

    def run_aggregation_sbx(
        self,
        *,
        iteration: int,
        read_charge_rate: Callable[[], Optional[float]],
        step_delay_sec: float,
        site_capacity_a: Optional[float] = None,
        read_duty_cycle_pct: Optional[Callable[[], Optional[float]]] = None,
        request_ceiling_override: Optional[
            Callable[[dict[str, Any]], Optional[float]]
        ] = None,
        read_charge_rate_detail: Optional[
            Callable[[], Optional[dict[str, Any]]]
        ] = None,
        read_panel_total_a: Optional[Callable[[], Optional[float]]] = None,
        refresh_panel_total_a: Optional[Callable[[], Optional[float]]] = None,
        case_ids: Optional[CaseIdFactory] = None,
    ) -> dict[str, Any]:
        """Phase 5 (SBX-driven): step LB2 up until the panel->drive charge_rate
        (read from the SBX log) drops (throttle engaged), then keeps stepping
        until it reaches 6 A and finally 0 A.

        When ``charge_rate`` drops but EV pilot duty has not yet tracked it,
        waits up to ``duty_follow_timeout_sec`` before the next LB2 step so the
        EV load can follow PWM (emits ``duty_settle_*`` / ``duty_vs_charge_rate``).

        When Dynamic LB2 hits the configured ceiling but pilot duty is still
        above ``full_throttle_duty_pct`` (default 10%), ``request_ceiling_override``
        may raise the ceiling so stepping can continue.

        After each LB2 step, waits ``metrics_settle_sec`` then re-reads SBX +
        (optionally fresh) feeder amps so JAMA balance metrics are aligned.
        """
        params = self.params
        ids = case_ids or CaseIdFactory()

        def _cr_detail() -> tuple[Optional[float], Optional[dict[str, Any]]]:
            detail: Optional[dict[str, Any]] = None
            if read_charge_rate_detail is not None:
                try:
                    detail = read_charge_rate_detail()
                except Exception:
                    detail = None
            if detail and detail.get("amps") is not None:
                return float(detail["amps"]), detail
            cr_val = read_charge_rate()
            return (float(cr_val) if cr_val is not None else None), detail

        def _wait_for_observed(
            cr_v: Optional[float],
            detail: Optional[dict[str, Any]],
            obs: Optional[float],
        ) -> tuple[Optional[float], Optional[dict[str, Any]], Optional[float]]:
            """Poll Modbus up to observed_wait_sec for observed_charge_rate_a."""
            if _detail_is_drive_status_with_observed(detail):
                return cr_v, detail, _observed_amps_from_detail(detail)
            wait_s = max(0.0, float(getattr(params, "observed_wait_sec", 8.0) or 0.0))
            if wait_s <= 0:
                return cr_v, detail, obs
            deadline = time.monotonic() + wait_s
            self._emit(
                "observed_wait",
                iteration=iteration,
                wait_sec=wait_s,
                message=(
                    f"Waiting up to {wait_s:g}s for MsgDriveStatus "
                    f"observed_charge_rate_a"
                ),
            )
            while time.monotonic() < deadline:
                if self._stop_check():
                    break
                self._sleep(0.5)
                cr_v, detail = _cr_detail()
                if _detail_is_drive_status_with_observed(detail):
                    obs = _observed_amps_from_detail(detail)
                    break
            return cr_v, detail, obs

        def _panel_total(*, fresh: bool = False) -> Optional[float]:
            if fresh and refresh_panel_total_a is not None:
                try:
                    val = refresh_panel_total_a()
                    return float(val) if val is not None else None
                except Exception:
                    pass
            if read_panel_total_a is None:
                return None
            try:
                val = read_panel_total_a()
                return float(val) if val is not None else None
            except Exception:
                return None

        def _settle_and_sample() -> tuple[
            Optional[float],
            Optional[dict[str, Any]],
            Optional[float],
            Optional[float],
            Optional[float],
            Optional[float],
        ]:
            settle = max(0.0, float(getattr(params, "metrics_settle_sec", 2.5) or 0.0))
            if settle > 0:
                self._emit(
                    "metrics_settle",
                    iteration=iteration,
                    settle_sec=settle,
                    message=f"Settling {settle:g}s for fresh panel/LB2/SBX samples",
                )
                self._sleep(settle)
            cr_v, detail = _cr_detail()
            obs = _observed_amps_from_detail(detail)
            cr_v, detail, obs = _wait_for_observed(cr_v, detail, obs)
            panel = _panel_total(fresh=True)
            meter = None
            achieved_v = None
            try:
                snap = self.lb2.meter_snapshot() or {}
                raw_m = snap.get("total_amps")
                meter = float(raw_m) if raw_m is not None else None
                if meter is not None and meter <= 0:
                    meter = None
            except Exception:
                meter = None
            try:
                desired = select_loads_panel_total(
                    self.commanded_amps,
                    params.panel_channels,
                    channel_voltages=params.channel_voltages,
                )
                achieved_v = loads_to_amps(
                    desired, channel_voltages=params.channel_voltages
                )
            except Exception:
                achieved_v = None
            return cr_v, detail, obs, panel, meter, achieved_v

        baseline, _baseline_detail = _cr_detail()
        saw_charge_rate = baseline is not None
        # Do not invent breaker_rating as baseline — that masks a dead SBX feed.
        if baseline is None:
            wait_deadline = time.monotonic() + 60.0
            self._emit(
                "aggregation_waiting_sbx",
                iteration=iteration,
                message="Waiting up to 60s for first SBX charge_rate before aggregation",
            )
            while time.monotonic() < wait_deadline:
                if self._stop_check():
                    return {
                        "ok": False,
                        "engaged": False,
                        "reason": "cancelled",
                        "saw_charge_rate": False,
                    }
                self._sleep(1.0)
                baseline, _baseline_detail = _cr_detail()
                if baseline is not None:
                    saw_charge_rate = True
                    break
        if baseline is None:
            self._emit(
                "aggregation_no_sbx",
                iteration=iteration,
                message=(
                    "No SBX charge_rate in Modbus log — cannot run load control. "
                    "Check FTDI serial (DRIVE_SBX_SERIAL) vs the adapter in use."
                ),
            )
            return {
                "ok": False,
                "engaged": False,
                "reached_6a": False,
                "reached_0a": False,
                "reason": "no_sbx_charge_rate",
                "saw_charge_rate": False,
                "baseline_charge_rate_a": None,
                "commanded_amps": self.commanded_amps,
            }
        self.baseline_panel_rate = baseline
        self._emit(
            "aggregation_start",
            iteration=iteration,
            baseline_charge_rate_a=baseline,
            breaker_rating_a=params.breaker_rating_a,
            site_capacity_a=site_capacity_a,
            message=(
                f"Stepping LB2 up (baseline charge_rate {baseline} A) until panel throttles"
            ),
        )

        soft_ceiling = max(0.0, float(params.max_lb2_amps))
        hw_max = max_panel_amps(
            params.panel_channels,
            channel_voltages=params.channel_voltages,
        )
        ceiling = (
            min(soft_ceiling, hw_max) if hw_max > 0 and soft_ceiling > 0 else soft_ceiling
        )
        step = max(0.5, float(params.aggregation_step_a))
        delay = max(0.5, float(step_delay_sec) or float(params.loop_interval_sec))
        deadband = float(params.deadband_a)
        full_duty = float(params.full_throttle_duty_pct)

        engaged = False
        reached_6 = False
        reached_0 = False
        load_increase_noted = False
        target = float(self.commanded_amps)
        duty_pct: Optional[float] = None
        last_achieved = -1.0
        prev_cr: Optional[float] = float(baseline) if baseline is not None else None
        step_index = 0

        self._emit(
            "aggregation_capacity",
            iteration=iteration,
            soft_ceiling_a=soft_ceiling,
            hardware_max_amps=hw_max,
            effective_ceiling_a=ceiling,
            panel_channels=list(params.panel_channels),
            message=(
                f"LB2 aggregation ceiling {ceiling:.1f} A "
                f"(UI max {soft_ceiling:.1f} A, hardware max ~{hw_max:.1f} A "
                f"on channels {list(params.panel_channels)})"
            ),
        )

        # No hard aggregation timeout — run until fully throttled, operator
        # declines a ceiling raise, cancelled, hardware saturated, or 0 A.
        while True:
            if self._stop_check():
                return {"ok": False, "phase": "aggregation", "reason": "cancelled"}

            at_cap = self.commanded_amps >= ceiling - 0.01
            stepped = False
            if not at_cap:
                target = min(ceiling, target + step)
                self._apply_absolute(target, iteration=iteration)
                self._sleep(delay)
                step_index += 1
                stepped = True

            if stepped:
                cr, cr_detail, observed_a, panel_total, meter_a, achieved = (
                    _settle_and_sample()
                )
            else:
                # At ceiling — light refresh without extra settle delay.
                cr, cr_detail = _cr_detail()
                observed_a = _observed_amps_from_detail(cr_detail)
                cr, cr_detail, observed_a = _wait_for_observed(cr, cr_detail, observed_a)
                panel_total = _panel_total(fresh=False)
                meter_a = None
                achieved = None
                try:
                    snap = self.lb2.meter_snapshot() or {}
                    raw_m = snap.get("total_amps")
                    meter_a = float(raw_m) if raw_m is not None else None
                    if meter_a is not None and meter_a <= 0:
                        meter_a = None
                except Exception:
                    meter_a = None
                try:
                    desired = select_loads_panel_total(
                        self.commanded_amps,
                        params.panel_channels,
                        channel_voltages=params.channel_voltages,
                    )
                    achieved = loads_to_amps(
                        desired, channel_voltages=params.channel_voltages
                    )
                except Exception:
                    achieved = None
            if cr is not None:
                saw_charge_rate = True
            prev_duty_pct = duty_pct
            if read_duty_cycle_pct is not None:
                try:
                    duty_pct = read_duty_cycle_pct()
                except Exception:
                    duty_pct = None

            duty_implied = _duty_implied_amps(duty_pct)
            cr_dropped = (
                cr is not None
                and baseline is not None
                and float(cr) < float(baseline) - deadband
            )
            duty_tracks = _duty_tracks_charge_rate(
                charge_rate_a=cr,
                duty_pct=duty_pct,
                full_throttle_duty_pct=full_duty,
                tolerance_a=float(
                    getattr(params, "duty_follow_tolerance_a", 3.0) or 3.0
                ),
            )
            duty_lag = bool(cr_dropped and not duty_tracks)
            obs_lag = (
                cr_dropped
                and observed_a is not None
                and cr is not None
                and float(observed_a) > float(cr) + float(
                    getattr(params, "duty_follow_tolerance_a", 3.0) or 3.0
                )
            )

            # Land in run jsonl (remote-safe) — used to diagnose duty lag vs EV draw.
            self._emit(
                "duty_vs_charge_rate",
                iteration=iteration,
                step_index=step_index,
                commanded_amps=round(float(self.commanded_amps), 2),
                charge_rate_a=cr,
                observed_charge_rate_a=observed_a,
                baseline_charge_rate_a=baseline,
                duty_cycle_pct=duty_pct,
                prev_duty_cycle_pct=prev_duty_pct,
                duty_implied_a=duty_implied,
                cr_dropped=cr_dropped,
                duty_tracks=duty_tracks,
                duty_lag=duty_lag,
                observed_above_cr=bool(obs_lag),
                stepped=stepped,
                step_delay_sec=delay,
                message=(
                    f"duty vs charge_rate: cr={cr} A duty={duty_pct}"
                    + (f"% (~{duty_implied} A)" if duty_implied is not None else "%")
                    + (" LAG" if duty_lag else " ok")
                ),
            )

            # Hold next LB2 step until pilot duty tracks reduced charge_rate.
            if (
                duty_lag
                and read_duty_cycle_pct is not None
                and float(getattr(params, "duty_follow_timeout_sec", 0.0) or 0.0) > 0
            ):
                wait_s = float(params.duty_follow_timeout_sec)
                poll_s = max(
                    0.2, float(getattr(params, "duty_follow_poll_sec", 0.5) or 0.5)
                )
                tol_a = float(
                    getattr(params, "duty_follow_tolerance_a", 3.0) or 3.0
                )
                self._emit(
                    "duty_settle_start",
                    iteration=iteration,
                    charge_rate_a=cr,
                    duty_cycle_pct=duty_pct,
                    duty_implied_a=duty_implied,
                    timeout_sec=wait_s,
                    message=(
                        f"Waiting up to {wait_s:g}s for pilot duty to track "
                        f"charge_rate {cr} A (duty now {duty_pct}%)"
                    ),
                )
                deadline = time.monotonic() + wait_s
                settled = False
                while time.monotonic() < deadline:
                    if self._stop_check():
                        return {
                            "ok": False,
                            "phase": "aggregation",
                            "reason": "cancelled",
                        }
                    self._sleep(poll_s)
                    try:
                        duty_pct = read_duty_cycle_pct()
                    except Exception:
                        duty_pct = None
                    duty_implied = _duty_implied_amps(duty_pct)
                    if _duty_tracks_charge_rate(
                        charge_rate_a=cr,
                        duty_pct=duty_pct,
                        full_throttle_duty_pct=full_duty,
                        tolerance_a=tol_a,
                    ):
                        settled = True
                        break
                self._emit(
                    "duty_settle_ok" if settled else "duty_settle_timeout",
                    iteration=iteration,
                    charge_rate_a=cr,
                    duty_cycle_pct=duty_pct,
                    duty_implied_a=duty_implied,
                    timeout_sec=wait_s,
                    message=(
                        (
                            f"Pilot duty tracked charge_rate {cr} A "
                            f"(duty {duty_pct}%)"
                        )
                        if settled
                        else (
                            f"Pilot duty still lagging after {wait_s:g}s "
                            f"(cr={cr} A, duty={duty_pct}%) — continuing"
                        )
                    ),
                )

            fault = _fault_info_from_detail(cr_detail)

            self._emit(
                "aggregation_sample",
                iteration=iteration,
                charge_rate_a=cr,
                observed_charge_rate_a=observed_a,
                baseline_charge_rate_a=baseline,
                commanded_amps=round(self.commanded_amps, 2),
                achieved_amps=achieved,
                meter_amps=meter_a,
                panel_total_a=panel_total,
                hardware_max_amps=hw_max,
                site_capacity_a=site_capacity_a,
                duty_cycle_pct=duty_pct,
                duty_implied_a=duty_implied,
                duty_lag=duty_lag,
                fault_mask=(fault or {}).get("fault_mask"),
                message=(
                    f"LB2 cmd {self.commanded_amps:.1f} A"
                    + (f" / ach ~{achieved:.1f} A" if achieved is not None else "")
                    + (f" / meter {meter_a:.1f} A" if meter_a is not None else "")
                    + (f" / panel {panel_total:.1f} A" if panel_total is not None else "")
                    + f" -> charge_rate {cr} A"
                    + (
                        f" (obs {observed_a:g} A)"
                        if observed_a is not None
                        else " (obs not observed)"
                    )
                    + (
                        f" (duty {duty_pct:.1f}%)"
                        if duty_pct is not None
                        else ""
                    )
                    + (
                        f" FAULT {(fault or {}).get('fault_mask')}"
                        if fault
                        else ""
                    )
                ),
            )

            evidence_rows: list[dict[str, Any]] = []
            if cr_detail and cr_detail.get("evidence"):
                evidence_rows.append(
                    {
                        "stream": "modbus",
                        "ts": cr_detail.get("ts") or "",
                        "line": str(cr_detail.get("evidence"))[:400],
                        "label": cr_detail.get("source") or "charge_rate",
                    }
                )
            if fault and fault.get("evidence"):
                evidence_rows.append(
                    {
                        "stream": "modbus",
                        "ts": fault.get("ts") or "",
                        "line": str(fault.get("evidence"))[:400],
                        "label": "sbx_fault",
                    }
                )
            if engaged:
                if cr is None:
                    step_ok = False
                    expected = (
                        f"charge_rate ≤ {(prev_cr or 0) + deadband:.1f} A "
                        f"(throttling active; step +{step:g} A)"
                    )
                    observed = "no charge_rate evidence"
                elif prev_cr is None:
                    step_ok = bool(evidence_rows)
                    expected = "charge_rate evidence present while throttling"
                    observed = f"{cr} A"
                else:
                    step_ok = float(cr) <= float(prev_cr) + deadband
                    expected = (
                        f"charge_rate ≤ {float(prev_cr) + deadband:.1f} A "
                        f"(prev {prev_cr} A, step +{step:g} A LB2)"
                    )
                    observed = f"{cr} A"
                if not evidence_rows:
                    step_ok = False
                    observed = f"{observed} (no Modbus line)"
            else:
                step_ok = bool(evidence_rows) and cr is not None
                expected = (
                    f"Modbus charge_rate evidence at LB2 {self.commanded_amps:.0f} A "
                    f"(step +{step:g} A; pre-throttle record)"
                )
                observed = (
                    f"{cr} A" if cr is not None else "no charge_rate"
                ) + (" + log line" if evidence_rows else " (no log line)")

            if fault:
                step_ok = False
                expected = (
                    f"{expected}; fault_mask=0x0 (no active Drive fault)"
                )
                observed = (
                    f"{observed} · SBX FAULT "
                    f"mask={fault.get('fault_mask') or '—'} "
                    f"state={fault.get('charging_state') or '—'}"
                )
                _emit_sbx_fault_step_case(
                    self._emit,
                    ids=ids,
                    section="Throttle — SBX fault (aggregate)",
                    title=(
                        f"SBX fault during aggregate at LB2 → "
                        f"{self.commanded_amps:.0f} A"
                    ),
                    fault=fault,
                    iteration=iteration,
                    commanded_amps=float(self.commanded_amps),
                )

            lb2_a = round(float(self.commanded_amps), 2)
            metrics = {
                "sbx_request_a": cr,
                "observed_charge_rate_a": _observed_metric_value(observed_a),
                "panel_total_a": panel_total,
                "lb2_load_a": lb2_a,
                "lb2_meter_a": meter_a,
                "lb2_achieved_a": achieved,
            }
            if fault:
                metrics["fault_mask"] = fault.get("fault_mask") or fault.get(
                    "charging_state"
                )
            # #region agent log
            _debug_bal_log(
                hypothesis_id="H3",
                location="load_controller.py:run_aggregation_sbx:step",
                message="aggregate step meter sample",
                data={
                    "iteration": iteration,
                    "step_index": step_index,
                    "commanded_amps": self.commanded_amps,
                    "meter_a": meter_a,
                    "achieved": achieved,
                    "panel_total": panel_total,
                    "observed_a": observed_a,
                    "cr": cr,
                    "fault": fault,
                },
            )
            # #endregion
            observed_full = (
                f"SBX request {cr if cr is not None else '—'} A"
                + _observed_text(observed_a)
                + f" · panel total {panel_total if panel_total is not None else '—'} A"
                + f" · LB2 load {lb2_a} A"
                + (
                    f" · LB2 meter {meter_a:.1f} A"
                    if meter_a is not None
                    else " · LB2 meter —"
                )
            )
            if fault:
                observed_full += (
                    f" · FAULT mask={fault.get('fault_mask') or '—'} "
                    f"state={fault.get('charging_state') or '—'}"
                )
            if not step_ok and "no Modbus" in observed:
                observed_full += " — no Modbus evidence"

            emit_jama_case(
                self._emit,
                case_id=ids.next("THR"),
                section="Throttle — aggregate steps",
                title=(
                    f"Aggregate step {max(step_index, 1)}: LB2 → "
                    f"{self.commanded_amps:.0f} A (step +{step:g} A)"
                ),
                signal="charge_rate_a / observed / panel_total / LB2 load+meter",
                expected=expected,
                observed=observed_full,
                sources="modbus, gateway, lb2",
                result="pass" if step_ok else "fail",
                evidence=evidence_rows,
                show_evidence=True,
                metrics=metrics,
                iteration=iteration,
                commanded_amps=lb2_a,
                charge_rate_a=cr,
                observed_charge_rate_a=observed_a,
                panel_total_a=panel_total,
                baseline_charge_rate_a=baseline,
                step_a=step,
                engaged=engaged,
            )
            _emit_current_balance_case(
                self._emit,
                ids=ids,
                section="Throttle — current balance",
                title=(
                    f"Current balance after LB2 → {self.commanded_amps:.0f} A "
                    f"(step +{step:g} A)"
                ),
                panel_total=panel_total,
                lb2_commanded_a=lb2_a,
                lb2_meter_a=meter_a,
                lb2_achieved_a=achieved,
                observed_a=observed_a,
                request_a=cr,
                tolerance_a=float(getattr(params, "balance_tolerance_a", 3.0) or 3.0),
                evidence=evidence_rows,
                iteration=iteration,
            )

            if not engaged and cr is not None and cr < (baseline - deadband):
                engaged = True
                self._emit(
                    "throttle_engaged",
                    iteration=iteration,
                    charge_rate_a=cr,
                    baseline_charge_rate_a=baseline,
                    commanded_amps=round(float(self.commanded_amps), 2),
                    achieved_amps=achieved,
                    meter_amps=meter_a,
                    duty_cycle_pct=duty_pct,
                    message=(
                        "Throttling started — "
                        f"panel load cmd={self.commanded_amps:.1f} A"
                        + (f" / meter {meter_a:.1f} A" if meter_a is not None else "")
                        + (f", duty {duty_pct:.1f}%" if duty_pct is not None else "")
                        + f", drive charge_rate {cr} A"
                    ),
                )
            elif (
                not engaged
                and not load_increase_noted
                and achieved is not None
                and achieved > 1.0
            ):
                load_increase_noted = True
                self._emit(
                    "panel_load_increased",
                    iteration=iteration,
                    commanded_amps=round(float(self.commanded_amps), 2),
                    achieved_amps=achieved,
                    meter_amps=meter_a,
                    charge_rate_a=cr,
                    duty_cycle_pct=duty_pct,
                    message=(
                        "Panel current increased as expected — "
                        "load will be further increased to engage throttling"
                    ),
                )

            if engaged and cr is not None:
                if not reached_6 and cr <= 6:
                    reached_6 = True
                    self._emit(
                        "throttle_step_6a",
                        iteration=iteration,
                        charge_rate_a=cr,
                        message=f"Panel throttled drive request down to {cr} A (<=6 A)",
                    )
                if cr <= 0:
                    reached_0 = True
                    self._emit(
                        "throttle_zero",
                        iteration=iteration,
                        charge_rate_a=cr,
                        message="Panel throttled drive request to 0 A",
                    )
                    break

            if cr is not None:
                prev_cr = float(cr)

            hw_saturated = (
                achieved is not None
                and last_achieved >= 0
                and achieved <= last_achieved + 0.25
                and self.commanded_amps >= max(last_achieved, achieved) + step - 0.01
            )
            if achieved is not None:
                last_achieved = float(achieved)

            at_cap = self.commanded_amps >= ceiling - 0.01 or hw_saturated
            if not at_cap:
                continue

            if reached_6 or (cr is not None and cr <= 6):
                self._emit(
                    "aggregation_ceiling",
                    iteration=iteration,
                    commanded_amps=round(self.commanded_amps, 2),
                    max_lb2_amps=ceiling,
                    hardware_max_amps=hw_max,
                    achieved_amps=achieved,
                    duty_cycle_pct=duty_pct,
                    message=(
                        f"LB2 at ceiling {ceiling:.0f} A — charge_rate already ≤6 A"
                    ),
                )
                break

            if duty_pct is not None and duty_pct <= full_duty:
                self._emit(
                    "aggregation_ceiling",
                    iteration=iteration,
                    commanded_amps=round(self.commanded_amps, 2),
                    max_lb2_amps=ceiling,
                    hardware_max_amps=hw_max,
                    achieved_amps=achieved,
                    duty_cycle_pct=duty_pct,
                    message=(
                        f"LB2 at ceiling {ceiling:.0f} A — duty {duty_pct:.1f}% ≤ "
                        f"{full_duty:g}% (fully throttled)"
                    ),
                )
                break

            if request_ceiling_override is None:
                self._emit(
                    "aggregation_ceiling",
                    iteration=iteration,
                    commanded_amps=round(self.commanded_amps, 2),
                    max_lb2_amps=ceiling,
                    hardware_max_amps=hw_max,
                    achieved_amps=achieved,
                    duty_cycle_pct=duty_pct,
                    message=(
                        f"LB2 at ceiling {ceiling:.0f} A — cannot push further"
                        + (
                            f" (duty {duty_pct:.1f}%)"
                            if duty_pct is not None
                            else ""
                        )
                    ),
                )
                break

            # Always prompt — including at hardware max. Previously we ended
            # silently on hw saturation and the operator never saw a popup.
            at_hw = hw_max > 0 and self.commanded_amps >= hw_max - 0.5
            if at_hw:
                suggested = round(float(hw_max), 1)
            else:
                suggested = round(
                    min(
                        hw_max if hw_max > 0 else ceiling + 40,
                        ceiling + max(20.0, step * 4),
                    ),
                    1,
                )
            duty_note = (
                f" (duty {duty_pct:.1f}% > {full_duty:g}%)"
                if duty_pct is not None
                else " (duty unavailable)"
            )
            if at_hw:
                prompt_msg = (
                    f"LB2 at hardware max ~{hw_max:.1f} A on channels "
                    f"{list(params.panel_channels)} but Drive/CP not fully throttled"
                    f"{duty_note} — waiting for operator to Continue (sustain) or Stop"
                )
            else:
                prompt_msg = (
                    f"LB2 at ceiling {ceiling:.0f} A but Drive/CP not fully throttled"
                    f"{duty_note} — waiting for operator to raise ceiling or stop"
                )
            prompt = {
                "iteration": iteration,
                "commanded_amps": round(self.commanded_amps, 2),
                "achieved_amps": achieved,
                "meter_amps": meter_a,
                "max_lb2_amps": ceiling,
                "hardware_max_amps": hw_max,
                "at_hardware_max": at_hw,
                "charge_rate_a": cr,
                "duty_cycle_pct": duty_pct,
                "full_throttle_duty_pct": full_duty,
                "step_a": step,
                "suggested_ceiling_a": suggested,
                "message": prompt_msg,
            }
            self._emit("ceiling_override_prompt", **prompt)
            new_ceiling = request_ceiling_override(prompt)
            if at_hw:
                # Relays already fully on — raising soft ceiling cannot add amps.
                self._emit(
                    "aggregation_hardware_saturated",
                    iteration=iteration,
                    commanded_amps=round(self.commanded_amps, 2),
                    achieved_amps=achieved,
                    meter_amps=meter_a,
                    hardware_max_amps=hw_max,
                    duty_cycle_pct=duty_pct,
                    operator_continued=new_ceiling is not None,
                    message=(
                        f"LB2 hardware saturated at ~{hw_max:.1f} A on channels "
                        f"{list(params.panel_channels)} but duty still "
                        f"{duty_pct if duty_pct is not None else '—'}% "
                        f"(need ≤{full_duty:g}% for full throttle) — ending aggregation"
                    ),
                )
                break
            if new_ceiling is None:
                self._emit(
                    "ceiling_override_declined",
                    iteration=iteration,
                    max_lb2_amps=ceiling,
                    duty_cycle_pct=duty_pct,
                    message="Operator declined raising LB2 ceiling — ending aggregation",
                )
                break
            new_soft = float(new_ceiling)
            if new_soft <= ceiling + 0.01:
                self._emit(
                    "ceiling_override_declined",
                    iteration=iteration,
                    max_lb2_amps=ceiling,
                    duty_cycle_pct=duty_pct,
                    message=(
                        "New ceiling not higher than current effective ceiling — "
                        "ending aggregation"
                    ),
                )
                break
            soft_ceiling = new_soft
            params.max_lb2_amps = soft_ceiling
            ceiling = min(soft_ceiling, hw_max) if hw_max > 0 else soft_ceiling
            self._emit(
                "ceiling_override_accepted",
                iteration=iteration,
                max_lb2_amps=soft_ceiling,
                effective_ceiling_a=ceiling,
                hardware_max_amps=hw_max,
                duty_cycle_pct=duty_pct,
                message=(
                    f"LB2 ceiling raised to {soft_ceiling:.0f} A "
                    f"(effective {ceiling:.1f} A, hw max {hw_max:.1f} A) — "
                    "continuing aggregation"
                ),
            )

        if not engaged:
            reason = (
                "no_sbx_charge_rate" if not saw_charge_rate else "aggregation_ended"
            )
            self._emit(
                "aggregation_timeout",
                iteration=iteration,
                reason=reason,
                saw_charge_rate=saw_charge_rate,
                message=(
                    "No SBX charge_rate observed during aggregation"
                    if not saw_charge_rate
                    else "Aggregation ended without observed throttle on SBX charge_rate"
                ),
            )

        return {
            "ok": engaged,
            "engaged": engaged,
            "reached_6a": reached_6,
            "reached_0a": reached_0,
            "reason": (
                None
                if engaged
                else (
                    "no_sbx_charge_rate"
                    if not saw_charge_rate
                    else "aggregation_ended"
                )
            ),
            "saw_charge_rate": saw_charge_rate,
            "baseline_charge_rate_a": baseline,
            "commanded_amps": self.commanded_amps,
            "duty_cycle_pct": duty_pct,
        }

    def run_recovery_sbx(
        self,
        *,
        iteration: int,
        read_charge_rate: Callable[[], Optional[float]],
        step_delay_sec: float,
        read_charge_rate_detail: Optional[
            Callable[[], Optional[dict[str, Any]]]
        ] = None,
        read_panel_total_a: Optional[Callable[[], Optional[float]]] = None,
        refresh_panel_total_a: Optional[Callable[[], Optional[float]]] = None,
        read_duty_cycle_pct: Optional[Callable[[], Optional[float]]] = None,
        case_ids: Optional[CaseIdFactory] = None,
    ) -> dict[str, Any]:
        """Phase 8 (SBX-driven): step LB2 down symmetrically; each down-step the
        panel should raise the drive request again — monitor charge_rate rising
        back toward baseline until the load reaches 0.

        When ``read_duty_cycle_pct`` is provided, it is called after each step so
        EV contactors can retarget to the rising pilot duty (clean amp steps).
        """
        params = self.params
        ids = case_ids or CaseIdFactory()
        baseline = self.baseline_panel_rate or float(params.breaker_rating_a)
        step = max(0.5, float(params.aggregation_step_a))
        delay = max(0.5, float(step_delay_sec) or float(params.loop_interval_sec))
        deadband = float(params.deadband_a)
        self._emit(
            "recovery_start",
            iteration=iteration,
            baseline_charge_rate_a=baseline,
            commanded_amps=round(float(self.commanded_amps), 2),
            message=(
                f"Stepping LB2 down from {self.commanded_amps:.1f} A "
                f"(step −{step:g} A) — panel should restore drive request; "
                f"post-zero wait ≤{params.recovery_timeout_sec:g} s"
            ),
        )

        def _cr_detail() -> tuple[Optional[float], Optional[dict[str, Any]]]:
            detail: Optional[dict[str, Any]] = None
            if read_charge_rate_detail is not None:
                try:
                    detail = read_charge_rate_detail()
                except Exception:
                    detail = None
            if detail and detail.get("amps") is not None:
                return float(detail["amps"]), detail
            cr_val = read_charge_rate()
            return (float(cr_val) if cr_val is not None else None), detail

        def _wait_for_observed(
            cr_v: Optional[float],
            detail: Optional[dict[str, Any]],
            obs: Optional[float],
        ) -> tuple[Optional[float], Optional[dict[str, Any]], Optional[float]]:
            if _detail_is_drive_status_with_observed(detail):
                return cr_v, detail, _observed_amps_from_detail(detail)
            wait_s = max(0.0, float(getattr(params, "observed_wait_sec", 8.0) or 0.0))
            if wait_s <= 0:
                return cr_v, detail, obs
            deadline = time.monotonic() + wait_s
            self._emit(
                "observed_wait",
                iteration=iteration,
                wait_sec=wait_s,
                message=(
                    f"Waiting up to {wait_s:g}s for MsgDriveStatus "
                    f"observed_charge_rate_a"
                ),
            )
            while time.monotonic() < deadline:
                if self._stop_check():
                    break
                self._sleep(0.5)
                cr_v, detail = _cr_detail()
                if _detail_is_drive_status_with_observed(detail):
                    obs = _observed_amps_from_detail(detail)
                    break
            return cr_v, detail, obs

        def _panel_total(*, fresh: bool = False) -> Optional[float]:
            if fresh and refresh_panel_total_a is not None:
                try:
                    val = refresh_panel_total_a()
                    return float(val) if val is not None else None
                except Exception:
                    pass
            if read_panel_total_a is None:
                return None
            try:
                val = read_panel_total_a()
                return float(val) if val is not None else None
            except Exception:
                return None

        target = float(self.commanded_amps)
        prev_cr: Optional[float] = None
        step_index = 0
        # Step all the way to 0 — do NOT abort mid-ramp on a short wall-clock
        # timeout (that caused LB2 to jump 25 A → 0 A). Only operator stop ends
        # the ramp early. Post-zero baseline wait uses recovery_timeout_sec.
        while target > 0.05:
            if self._stop_check():
                return {"ok": False, "reason": "cancelled"}
            target = max(0.0, target - step)
            self._apply_absolute(target, iteration=iteration)
            self._sleep(delay)
            step_index += 1
            settle = max(0.0, float(getattr(params, "metrics_settle_sec", 2.5) or 0.0))
            if settle > 0:
                self._sleep(settle)
            cr, cr_detail = _cr_detail()
            observed_a = _observed_amps_from_detail(cr_detail)
            cr, cr_detail, observed_a = _wait_for_observed(cr, cr_detail, observed_a)
            panel_total = _panel_total(fresh=True)
            meter_a: Optional[float] = None
            achieved: Optional[float] = None
            try:
                snap = self.lb2.meter_snapshot() or {}
                raw_m = snap.get("total_amps")
                meter_a = float(raw_m) if raw_m is not None else None
                if meter_a is not None and meter_a <= 0:
                    meter_a = None
            except Exception:
                meter_a = None
            try:
                desired = select_loads_panel_total(
                    self.commanded_amps,
                    params.panel_channels,
                    channel_voltages=params.channel_voltages,
                )
                achieved = loads_to_amps(
                    desired, channel_voltages=params.channel_voltages
                )
            except Exception:
                achieved = None
            duty_pct: Optional[float] = None
            if read_duty_cycle_pct is not None:
                try:
                    duty_pct = read_duty_cycle_pct()
                except Exception:
                    duty_pct = None
            fault = _fault_info_from_detail(cr_detail)
            self._emit(
                "recovery_step",
                iteration=iteration,
                charge_rate_a=cr,
                observed_charge_rate_a=observed_a,
                commanded_amps=round(self.commanded_amps, 2),
                meter_amps=meter_a,
                achieved_amps=achieved,
                panel_total_a=panel_total,
                baseline_charge_rate_a=baseline,
                duty_cycle_pct=duty_pct,
                fault_mask=(fault or {}).get("fault_mask"),
                message=(
                    f"LB2 {self.commanded_amps:.1f} A"
                    + (f" / meter {meter_a:.1f} A" if meter_a is not None else "")
                    + f" -> panel charge_rate {cr} A"
                    + (
                        f" (obs {observed_a:g} A)"
                        if observed_a is not None
                        else " (obs not observed)"
                    )
                    + (
                        f" (duty {duty_pct:.1f}%)"
                        if duty_pct is not None
                        else ""
                    )
                    + (
                        f" FAULT {(fault or {}).get('fault_mask')}"
                        if fault
                        else ""
                    )
                ),
            )
            evidence_rows: list[dict[str, Any]] = []
            if cr_detail and cr_detail.get("evidence"):
                evidence_rows.append(
                    {
                        "stream": "modbus",
                        "ts": cr_detail.get("ts") or "",
                        "line": str(cr_detail.get("evidence"))[:400],
                        "label": cr_detail.get("source") or "charge_rate",
                    }
                )
            if fault and fault.get("evidence"):
                evidence_rows.append(
                    {
                        "stream": "modbus",
                        "ts": fault.get("ts") or "",
                        "line": str(fault.get("evidence"))[:400],
                        "label": "sbx_fault",
                    }
                )
            if cr is None:
                step_ok = False
                expected = (
                    f"charge_rate rising toward baseline {baseline} A "
                    f"(LB2 −{step:g} A)"
                )
                observed = "no charge_rate evidence"
            elif prev_cr is None:
                step_ok = bool(evidence_rows)
                expected = "Modbus charge_rate evidence on recovery step"
                observed = f"{cr} A"
            else:
                # Panel should restore (raise) drive request as LB2 falls.
                step_ok = float(cr) >= float(prev_cr) - deadband
                expected = (
                    f"charge_rate ≥ {float(prev_cr) - deadband:.1f} A "
                    f"(prev {prev_cr} A → toward baseline {baseline} A)"
                )
                observed = f"{cr} A"
            if not evidence_rows:
                step_ok = False
                observed = f"{observed} (no Modbus line)"

            if fault:
                step_ok = False
                expected = (
                    f"{expected}; fault_mask=0x0 (no active Drive fault)"
                )
                observed = (
                    f"{observed} · SBX FAULT "
                    f"mask={fault.get('fault_mask') or '—'} "
                    f"state={fault.get('charging_state') or '—'}"
                )
                _emit_sbx_fault_step_case(
                    self._emit,
                    ids=ids,
                    section="Throttle — SBX fault (recover)",
                    title=(
                        f"SBX fault during recover at LB2 → "
                        f"{self.commanded_amps:.0f} A"
                    ),
                    fault=fault,
                    iteration=iteration,
                    commanded_amps=float(self.commanded_amps),
                )

            lb2_a = round(float(self.commanded_amps), 2)
            observed_full = (
                f"SBX request {cr if cr is not None else '—'} A"
                + _observed_text(observed_a)
                + f" · panel total {panel_total if panel_total is not None else '—'} A"
                + f" · LB2 load {lb2_a} A"
                + (
                    f" · LB2 meter {meter_a:.1f} A"
                    if meter_a is not None
                    else " · LB2 meter —"
                )
            )
            if fault:
                observed_full += (
                    f" · FAULT mask={fault.get('fault_mask') or '—'} "
                    f"state={fault.get('charging_state') or '—'}"
                )

            emit_jama_case(
                self._emit,
                case_id=ids.next("REC"),
                section="Throttle — recover steps",
                title=(
                    f"Recover step {step_index}: LB2 → "
                    f"{self.commanded_amps:.0f} A (step −{step:g} A)"
                ),
                signal="charge_rate_a / observed / panel_total / LB2 load+meter",
                expected=expected,
                observed=observed_full,
                sources="modbus, gateway, lb2",
                result="pass" if step_ok else "fail",
                evidence=evidence_rows,
                show_evidence=True,
                metrics={
                    "sbx_request_a": cr,
                    "observed_charge_rate_a": _observed_metric_value(observed_a),
                    "panel_total_a": panel_total,
                    "lb2_load_a": lb2_a,
                    "lb2_meter_a": (
                        round(float(meter_a), 2) if meter_a is not None else None
                    ),
                    "lb2_achieved_a": achieved,
                    **(
                        {
                            "fault_mask": fault.get("fault_mask")
                            or fault.get("charging_state")
                        }
                        if fault
                        else {}
                    ),
                },
                iteration=iteration,
                commanded_amps=lb2_a,
                charge_rate_a=cr,
                observed_charge_rate_a=observed_a,
                panel_total_a=panel_total,
                baseline_charge_rate_a=baseline,
                step_a=step,
            )
            _emit_current_balance_case(
                self._emit,
                ids=ids,
                section="Throttle — current balance (recover)",
                title=(
                    f"Current balance after recover LB2 → {self.commanded_amps:.0f} A "
                    f"(step −{step:g} A)"
                ),
                panel_total=panel_total,
                lb2_commanded_a=lb2_a,
                lb2_meter_a=meter_a,
                lb2_achieved_a=achieved,
                observed_a=observed_a,
                request_a=cr,
                tolerance_a=float(getattr(params, "balance_tolerance_a", 3.0) or 3.0),
                evidence=evidence_rows,
                iteration=iteration,
            )
            if cr is not None:
                prev_cr = float(cr)

        # Ensure fully off, then wait for charge_rate to rise toward baseline.
        self._apply_absolute(0.0, iteration=iteration)
        wait_deadline = time.monotonic() + float(params.recovery_timeout_sec)
        cr, _final_detail = _cr_detail()
        while time.monotonic() < wait_deadline:
            if self._stop_check():
                return {"ok": False, "reason": "cancelled"}
            cr, _final_detail = _cr_detail()
            if cr is not None and cr >= baseline * 0.85:
                break
            self._sleep(min(2.0, delay))

        recovered = cr is not None and cr >= baseline * 0.85
        if recovered:
            self._emit(
                "recovery_verified",
                iteration=iteration,
                charge_rate_a=cr,
                baseline_charge_rate_a=baseline,
                message=f"Recovery verified — drive request back to {cr} A",
            )
        else:
            self._emit(
                "recovery_timeout",
                iteration=iteration,
                charge_rate_a=cr,
                baseline_charge_rate_a=baseline,
                commanded_amps=round(self.commanded_amps, 2),
                message=(
                    "LB2 stepped to 0 A but charge_rate did not return to ≥85% of "
                    f"baseline {baseline} A within {params.recovery_timeout_sec:g} s"
                    + (f" (last charge_rate={cr} A)" if cr is not None else "")
                ),
            )
        return {"ok": recovered, "charge_rate_a": cr, "commanded_amps": self.commanded_amps}

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + float(seconds)
        while time.monotonic() < end:
            if self._stop_check():
                return
            time.sleep(min(0.25, end - time.monotonic()))

    def run_recovery(
        self,
        *,
        iteration: int,
        read_setpoint: Callable[[], SetpointReading],
    ) -> dict[str, Any]:
        """Phase 8: remove LB2 load and check recovery toward baselines."""
        deadline = time.monotonic() + float(self.params.recovery_timeout_sec)
        self._emit(
            "recovery_start",
            iteration=iteration,
            message="Ramping LB2 to zero for recovery",
        )
        while self.commanded_amps > 0.05 and time.monotonic() < deadline:
            if self._stop_check():
                return {"ok": False, "reason": "cancelled"}
            self.apply_commanded_amps(0.0, iteration=iteration)
            time.sleep(max(0.2, float(self.params.loop_interval_sec)))
        self.apply_commanded_amps(0.0, iteration=iteration)

        recovered = False
        while time.monotonic() < deadline:
            if self._stop_check():
                return {"ok": False, "reason": "cancelled"}
            reading = read_setpoint()
            recovered_pilot = True
            if self.baseline_pilot_adv and reading.pilot_adv_current_a is not None:
                recovered_pilot = (
                    reading.pilot_adv_current_a
                    >= self.baseline_pilot_adv * 0.85
                )
            recovered_duty = True
            if self.baseline_duty and reading.duty_cycle_pct is not None:
                recovered_duty = reading.duty_cycle_pct >= self.baseline_duty * 0.85
            if recovered_pilot and recovered_duty:
                recovered = True
                self._emit(
                    "recovery_verified",
                    iteration=iteration,
                    pilot_adv_current_a=reading.pilot_adv_current_a,
                    duty_cycle_pct=reading.duty_cycle_pct,
                    panel_charge_rate_a=reading.panel_charge_rate_a,
                    message="Recovery verified toward pre-throttle baselines",
                )
                break
            time.sleep(max(0.2, float(self.params.loop_interval_sec)))

        if not recovered:
            self._emit(
                "recovery_timeout",
                iteration=iteration,
                message="Recovery window ended without full baseline return",
            )
        return {"ok": recovered, "commanded_amps": self.commanded_amps}
