"""Charge-point one-click orchestration — EV Simulator + optional Load Bank-2.

Decommission is intentionally NOT part of this pipeline (separate dashboard/CLI action).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from drive_automation.artifacts import build_run_artifact
from drive_automation.clients import DashboardError, EvClient, Lb2Client
from drive_automation.commissioning import CommissionParams, commission_drive
from drive_automation.config import DriveConfig
from drive_automation.timeutil import now_local
from drive_automation.gateway import (
    GatewayError,
    GatewayShell,
    build_gateway_shell_from_env,
)
from drive_automation.jama_cases import CaseIdFactory, emit_jama_case, emit_sbx_pair_cases
from drive_automation.jama_pipeline import (
    JAMA_PIPELINE_PHASES,
    restore_jama_state,
    run_selected_jama_cases,
    snapshot_jama_state,
)
from drive_automation.load_controller import LoadController, LoadControllerParams
from drive_automation.load_selection import (
    DEFAULT_VOLTAGES,
    normalize_channel_voltages,
    select_loads_panel_total,
)
from drive_automation.panel_observe import PanelObserver
from drive_automation.telemetry import TelemetryLogger

# Throttle / load-bank one-click pipeline (no advertise/comms JAMA cases).
PIPELINE_PHASES = (
    "capture",
    "commission",
    "post_commission_verify",
    "boot",
    "handshake",
    "panel_ratings",
    "charge",
    "aggregate",
    "throttle",
    "verify",
    "recover",
)

@dataclass


class TestParams:
    charger_label: str
    test_name: str
    notes: str = ""
    ev_max_amps: int = 30
    lb2_max_amps: float = 0.0
    throttle_duration_sec: float = 60.0  # retained as sustain safety bound
    recovery_duration_sec: float = 30.0
    iterations: int = 1
    lb2_ramp_step_interval_sec: float = 5.0
    lb2_step_a: float = 5.0
    lb2_start_delay_sec: float = 10.0
    use_lb2: bool = True
    panel_channels: tuple[int, ...] = (1, 2, 3, 4)
    lb2_channel_voltages: dict[int, str] = field(
        default_factory=lambda: dict(DEFAULT_VOLTAGES)
    )
    # One-click Phase 1
    run_commission: bool = False
    drive_serial: str = ""
    breaker_rating_a: float = 0.0  # required — the commissioned drive current
    # DER cb instance for JAMA cases (optional if commission ran).
    breaker_instance_id: Optional[int] = None
    # "throttle" = LB2 load pipeline; "jama" = EV+gateway JAMA cases only.
    pipeline_mode: str = "throttle"
    # None / empty → all JAMA cases; else subset from case_selection aliases.
    jama_cases: Optional[tuple[str, ...]] = None
    top_position: Optional[int] = None
    bottom_position: Optional[int] = None
    drive_top_tab: Optional[int] = None
    drive_bottom_tab: Optional[int] = None
    drive_timezone: str = "America/Los_Angeles"
    # Single user-provided delay applied between every phase.
    phase_delay_sec: float = 0.0
    # EV draws this fraction of the commissioned drive current (set once).
    ev_draw_fraction: float = 0.8
    # Panel main capacity (site utility rating / limiting current).
    site_capacity_a: Optional[float] = None
    conductor_rating_a: Optional[float] = None
    busbar_rating_a: Optional[float] = None
    feeder_breaker_rating_a: Optional[float] = None
    panel_limit_a: Optional[float] = None
    throttle_threshold_a: Optional[float] = None
    site_utility_rating_ma: int = 125000
    conductor_rating_ma: int = 100000
    # Closed-loop controller
    load_loop_interval_sec: float = 1.0
    load_deadband_a: float = 0.75
    # Static safety bounds (not exposed in the dashboard UI).
    aggregation_timeout_sec: float = 180.0
    sustain_timeout_sec: float = 90.0
    use_dynamic_load: bool = True
    full_throttle_duty_pct: float = 10.0
    # Hold next LB2 step until EV pilot duty tracks reduced charge_rate (remote jsonl).
    duty_follow_timeout_sec: float = 15.0
    duty_follow_tolerance_a: float = 3.0
    ceiling_override_timeout_sec: float = 300.0


@dataclass


class AvailabilityResult:
    ev: dict[str, Any]
    lb2: dict[str, Any]
    ev_available: bool
    lb2_available: bool
    lb2_operator: str = ""

    @property
    def ev_in_use(self) -> bool:
        return bool(self.ev.get("in_use"))

    @property
    def lb2_in_use(self) -> bool:
        return bool(self.lb2.get("in_use"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ev": self.ev,
            "lb2": self.lb2,
            "ev_available": self.ev_available,
            "lb2_available": self.lb2_available,
            "lb2_operator": self.lb2_operator,
        }


def check_availability(config: DriveConfig) -> AvailabilityResult:
    ev = EvClient(config.ev_url, config.operator, config.ev_password)
    lb2 = Lb2Client(config.lb2_url, config.operator, config.lb2_password)
    ev_status = ev.live_status(timeout=5.0)
    lb2_status = lb2.live_status(timeout=5.0)
    return AvailabilityResult(
        ev=ev_status,
        lb2=lb2_status,
        ev_available=not bool(ev_status.get("in_use")),
        lb2_available=not bool(lb2_status.get("in_use")),
        lb2_operator=str(lb2_status.get("operator") or ""),
    )


class ThrottleTestOrchestrator:
    def __init__(
        self,
        config: DriveConfig,
        params: TestParams,
        emit: Optional[Callable[..., None]] = None,
        log_path: Optional[Path] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.config = config
        self.params = params
        self._phase = "idle"
        self._iteration = 0
        self._commanded_lb2_amps = 0.0
        self._phase_started_at = time.time()
        self._phase_duration_sec: Optional[float] = None
        self._stop_check = stop_check or (lambda: False)
        self.ev: Optional[EvClient] = None
        self.lb2: Optional[Lb2Client] = None
        self._log_path = Path(log_path) if log_path else None
        self.last_artifact: Optional[dict[str, Any]] = None
        self._test_name_used = params.test_name
        self._ev_zip_bytes: Optional[bytes] = None
        self._ev_zip_name = ""
        pilot_path = None
        if self._log_path is not None:
            pilot_path = self._log_path.with_name(f"{self._log_path.stem}_pilot.jsonl")
        self._pilot_log_path = pilot_path
        # Multi-stream lab capture (pilot / EV loads / LB2 / panel).
        self._lab_capture = None
        if self._log_path is not None:
            from drive_automation.lab_capture import LabStreamCapture

            self._lab_capture = LabStreamCapture(
                self._log_path.parent,
                session_id=self._log_path.stem,
                tz_name=config.drive_timezone,
            )
            self._lab_capture.attach_automation()
            self._pilot_log_path = self._lab_capture.paths["pilot"]
        self.telemetry = TelemetryLogger(
            interval_sec=config.telemetry_interval_sec,
            log_path=str(log_path) if log_path else None,
            pilot_log_path=self._pilot_log_path,
            tz_name=config.drive_timezone,
        )
        self._custom_emit = emit
        self._emit = emit if emit is not None else self.telemetry.emit
        self.gateway: Optional[GatewayShell] = None
        self.panel: Optional[PanelObserver] = None
        self.last_commission: Optional[dict[str, Any]] = None
        self.pipeline_phase = "idle"
        self._capture_session_dir: Optional[Path] = None
        self._site_capacity_a: Optional[float] = params.site_capacity_a
        self._panel_limit_a: Optional[float] = params.panel_limit_a
        self._feeder_watcher: Any = None
        self._ceiling_override_event = threading.Event()
        self._ceiling_override_response: Optional[dict[str, Any]] = None
        self._ceiling_override_pending = False
        self._ceiling_override_prompt: Optional[dict[str, Any]] = None
        self._case_ids = CaseIdFactory()
        self._sbx_abnormality_stop = threading.Event()
        self._sbx_abnormality_thread: Optional[threading.Thread] = None
        self._sbx_abnormality_hits: list[dict[str, Any]] = []
        # Armed EV draw cap (80% breaker); used when retargeting contactors to duty.
        self._ev_draw_cap_a: int = 0
        self._ev_retarget_amps: Optional[int] = None
        # Case 9 gate: set after throttle verify sees sustained charge_rate drop.
        self._throttle_verified: bool = False

    def _pipeline_steps(self) -> list[str]:
        mode = str(self.params.pipeline_mode or "throttle").strip().lower()
        if mode == "jama":
            return list(JAMA_PIPELINE_PHASES)
        return list(PIPELINE_PHASES)

    def _set_pipeline(self, name: str, **payload: Any) -> None:
        self.pipeline_phase = name
        msg = str(payload.pop("message", f"Pipeline phase: {name}"))
        self._emit(
            "pipeline_phase",
            phase=name,
            steps=self._pipeline_steps(),
            message=msg,
            **payload,
        )

    def _validate(
        self,
        check: str,
        *,
        ok: bool,
        message: str,
        expected: Any = None,
        actual: Any = None,
        detail: Any = None,
        hard_fail: bool = False,
    ) -> None:
        """Emit a user-visible validation checkpoint (manual-test equivalent).

        When ``hard_fail`` and not ``ok``, raises ``DashboardError`` with a
        detailed message so the failure card + stepper show the exact check.
        """
        payload: dict[str, Any] = {
            "check": check,
            "ok": bool(ok),
            "message": message,
        }
        if expected is not None:
            payload["expected"] = expected
        if actual is not None:
            payload["actual"] = actual
        if detail is not None:
            payload["detail"] = detail
        self._emit("validation_check", **payload)
        if hard_fail and not ok:
            bits = [message]
            if expected is not None:
                bits.append(f"expected={expected}")
            if actual is not None:
                bits.append(f"actual={actual}")
            if detail is not None:
                bits.append(
                    detail if isinstance(detail, str) else str(detail)
                )
            raise DashboardError(" | ".join(bits))

    def _set_phase(
        self,
        phase: str,
        iteration: Optional[int] = None,
        *,
        duration_sec: Optional[float] = None,
    ) -> None:
        self._phase = phase
        self._phase_started_at = time.time()
        self._phase_duration_sec = (
            float(duration_sec) if duration_sec is not None else None
        )
        if iteration is not None:
            self._iteration = iteration
        self._emit(
            "phase",
            phase=phase,
            iteration=self._iteration,
            duration_sec=self._phase_duration_sec,
            started_at=datetime.fromtimestamp(
                self._phase_started_at, tz=timezone.utc
            ).isoformat(),
            message=f"Phase: {phase}",
        )

    def _extra(self) -> dict[str, Any]:
        elapsed = max(0.0, time.time() - self._phase_started_at)
        remaining = None
        if self._phase_duration_sec is not None:
            remaining = max(0.0, self._phase_duration_sec - elapsed)
        return {
            "phase": self._phase,
            "pipeline_phase": self.pipeline_phase,
            "iteration": self._iteration,
            "lb2_commanded_amps": round(self._commanded_lb2_amps, 2),
            "use_lb2": self.params.use_lb2,
            "phase_elapsed_sec": round(elapsed, 1),
            "phase_duration_sec": self._phase_duration_sec,
            "phase_remaining_sec": (
                round(remaining, 1) if remaining is not None else None
            ),
        }

    def run(self) -> None:
        if not self.config.ev_password:
            raise DashboardError(
                "DRIVE_EV_PASSWORD or DRIVE_PASSWORD is not set "
                "(must be loadbank1 Pi SSH password)."
            )

        self.ev = EvClient(
            self.config.ev_url,
            self.config.operator,
            self.config.ev_password,
        )
        if self.params.use_lb2:
            if not self.config.lb2_password:
                raise DashboardError(
                    "DRIVE_LB2_PASSWORD or DRIVE_PASSWORD is not set "
                    "(must be loadbank2 Pi SSH password)."
                )
            self.lb2 = Lb2Client(
                self.config.lb2_url,
                self.config.operator,
                self.config.lb2_password,
            )

        mode = str(self.params.pipeline_mode or "throttle").strip().lower()
        if mode == "jama":
            # JAMA pipeline never uses LB2.
            self.params.use_lb2 = False
            self.lb2 = None

        self.telemetry.bind(self.ev, self.lb2, extra=self._extra)
        self._ev_logged_in = False
        self._lb2_logged_in = False
        self._require_gateway()

        try:
            if mode == "jama":
                self._run_jama_procedure()
            else:
                self._run_procedure()
        except Exception as exc:
            # Emit before finally/packaging so the HTML report includes the failure.
            self._emit(
                "run_failed",
                pipeline_phase=self.pipeline_phase,
                phase=self._phase,
                iteration=self._iteration,
                error=str(exc),
                message=str(exc),
                detail={
                    "exception_type": type(exc).__name__,
                    "site_capacity_a": self._site_capacity_a,
                    "lb2_commanded_amps": self._commanded_lb2_amps,
                    "capture_session_dir": str(self._capture_session_dir or ""),
                    "hint": (
                        "Open Failure detail below and the Validation checklist. "
                        "Each failed check maps to a manual step you would verify on the wall."
                    ),
                },
            )
            raise
        finally:
            # Export while still logged in, then safe-exit, then package full log.
            try:
                self._export_ev_test_zip()
            except Exception as exc:
                self._emit(
                    "ev_test_export_skipped",
                    error=str(exc),
                    message=f"EV test export skipped: {exc}",
                )
            self.telemetry.stop()
            if self._feeder_watcher is not None:
                try:
                    self._feeder_watcher.stop()
                except Exception:
                    pass
                self._feeder_watcher = None
            if self._ev_logged_in or self._lb2_logged_in:
                self._safe_exit_all(
                    ev_was_active=self._ev_logged_in,
                    lb2_was_active=self._lb2_logged_in,
                )
            # Stop local capture before packaging so report/copy is not racing
            # a still-growing SBX/UART session (and gateway mirror SSH loops).
            self._stop_sbx_abnormality_watch()
            try:
                import dashboard_logs as dlog

                dlog.stop_unified()
                dlog.stop_uart_capture()
            except Exception:
                pass
            self._package_artifacts()

    def _require_gateway(self) -> None:
        """Mandatory gateway SSH before any pipeline phase."""
        self.gateway = build_gateway_shell_from_env(
            host=self.config.gateway_ssh_host,
            user=self.config.gateway_ssh_user,
            password=self.config.gateway_ssh_password,
            drive_serial=self.params.drive_serial or self.config.drive_serial,
            identity_file=self.config.gateway_ssh_identity,
            auth_mode=self.config.gateway_ssh_auth,
            gateway_serial=self.config.gateway_serial,
            spanssh_config=self.config.gateway_spanssh_config,
        )
        if not self.gateway.available:
            raise DashboardError(
                "Gateway SSH is required. Prefer spanssh-config on the wall "
                "(~/.ssh/span/<GATEWAY_SERIAL>/spanssh-config) with GATEWAY_SERIAL set, "
                "or set GATEWAY_SSH_HOST + auth. Cannot start the pipeline."
            )
        probe = self.gateway.test_connection()
        if not probe.get("ok"):
            raise DashboardError(
                "Gateway SSH precheck failed — fix connectivity before starting. "
                f"{probe.get('message') or probe}"
            )
        self.panel = PanelObserver(self.gateway)
        auth = probe.get("auth") or self.gateway.mode
        if auth == "spanssh" or str(auth).endswith("spanssh"):
            target = self.gateway.gateway_serial or "spanssh"
            ok_msg = f"Gateway SSH OK ({target}) via spanssh"
        else:
            target = f"{self.gateway.ssh_user}@{self.gateway.ssh_host}"
            ok_msg = f"Gateway SSH OK ({target}) via {auth}"
        self.gateway.on_command = self._on_gateway_command
        self._emit(
            "gateway_ready",
            host=self.gateway.ssh_host or None,
            gateway_serial=self.gateway.gateway_serial or None,
            auth=auth,
            message=ok_msg,
        )

    def _on_gateway_command(
        self,
        *,
        command: str,
        code: int,
        out: str,
        err: str,
        auth: str,
        **_extra: Any,
    ) -> None:
        """Print every gateway SSH command on the dashboard activity log."""
        cmd = str(command or "").strip()
        if not cmd:
            return
        tail = ((out or err or "").strip().replace("\n", " "))[:240]
        self._emit(
            "gateway_cmd",
            command=cmd,
            exit=int(code),
            auth=auth,
            output=tail,
            message=f"$ {cmd}  (exit {int(code)})",
        )

    def _run_procedure(self) -> None:
        assert self.ev is not None

        # Phase 1a — local capture first (SBX may be empty until after commission).
        self._set_pipeline(
            "capture",
            message="Starting local SBX/CAN/UART capture before commissioning",
        )
        self._start_local_capture(require_charge_rate=False)
        self._start_sbx_abnormality_watch()
        self._emit_capture_stream_status()
        self._phase_delay()

        # Phase 1b — commission (optional).
        if self.params.run_commission:
            self._run_commission_phase()
            self._validate(
                "commission_complete",
                ok=True,
                message="Commissioning was successful",
                actual=(self.last_commission or {}).get("discovery_state"),
            )
            self._emit(
                "commission_report",
                skipped=False,
                ok=True,
                breaker_rating_a=self.params.breaker_rating_a,
                top_position=self.params.top_position,
                bottom_position=self.params.bottom_position,
                services_restarted=True,
                discovery_state=(self.last_commission or {}).get("discovery_state"),
                discovery_successful=(
                    (self.last_commission or {}).get("discovery_state")
                    == "DISCOVERY_SUCCESSFUL"
                ),
                message="Commissioning was successful",
            )
            lc = self.last_commission or {}
            rating = lc.get("breaker_rating_a") or self.params.breaker_rating_a
            top = lc.get("top_tab_number") or self.params.top_position
            bottom = lc.get("bottom_tab_number") or self.params.bottom_position
            disc = str(lc.get("discovery_state") or "unknown")
            verify_raw = str((lc.get("details") or {}).get("breaker_verify") or "")
            emit_jama_case(
                self._emit,
                case_id=self._case_ids.next("COM"),
                section="Commission",
                title="Commissioned breaker rating and positions",
                signal="breaker_rating_a / tab positions",
                expected=(
                    f"rating {self.params.breaker_rating_a} A @ tabs "
                    f"{self.params.top_position}/{self.params.bottom_position}"
                ),
                observed=f"rating {rating} A @ tabs {top}/{bottom}",
                sources="gateway",
                result="pass",
                evidence=[
                    {
                        "stream": "gateway",
                        "ts": "",
                        "label": "commission",
                        "line": (
                            f"DER breaker instance={lc.get('breaker_instance_id')} "
                            f"BC ids top={lc.get('top_bc_instance')} "
                            f"bottom={lc.get('bottom_bc_instance')} "
                            f"rating={rating} A tabs={top}/{bottom}"
                        ),
                    }
                ]
                + (
                    [
                        {
                            "stream": "gateway",
                            "ts": "",
                            "label": "cb get-state",
                            "line": verify_raw[:400],
                        }
                    ]
                    if verify_raw
                    else []
                ),
                metrics={
                    "breaker_rating_a": rating,
                    "top_position": top,
                    "bottom_position": bottom,
                },
            )
            emit_jama_case(
                self._emit,
                case_id=self._case_ids.next("COM"),
                section="Commission",
                title="Drive discovery success",
                signal="discovery_state",
                expected="DISCOVERY_SUCCESSFUL",
                observed=disc,
                sources="gateway",
                result="pass" if disc == "DISCOVERY_SUCCESSFUL" else "fail",
                evidence=[
                    {
                        "stream": "gateway",
                        "ts": "",
                        "label": "discovery",
                        "line": (
                            f"discovery_state={disc}; "
                            f"{lc.get('message') or ''}"
                        )[:400],
                    }
                ],
                metrics={"discovery_state": disc},
            )
        else:
            self._set_pipeline("commission", skipped=True, message="Commission was skipped")
            self._emit(
                "commission_report",
                skipped=True,
                ok=True,
                message="Commission was skipped",
            )
            self._validate(
                "commission_skipped",
                ok=True,
                message="Commission was skipped (checkbox off)",
            )
            emit_jama_case(
                self._emit,
                case_id=self._case_ids.next("COM"),
                section="Commission",
                title="Commission Drive DER breaker + discovery",
                signal="commission",
                expected="skipped (checkbox off) or DISCOVERY_SUCCESSFUL",
                observed="skipped",
                sources="gateway",
                result="pass",
                evidence=[],
                show_evidence=False,
            )

        # Phase 1c — wait 60s and verify AssignBusId / DriveStartup (soft).
        self._set_pipeline(
            "post_commission_verify",
            message="Waiting 60s then verifying AssignDynamicBusId / DriveStartup in SBX log",
        )
        from drive_automation.sbx_verify import POST_COMMISSION_PAIRS, watch_pairs

        post_pairs = watch_pairs(
            self._capture_session_dir,
            POST_COMMISSION_PAIRS,
            window_sec=60.0,
            stop_check=self._stop_check,
            emit=self._emit,
            event_name="sbx_msg_verify",
        )
        emit_sbx_pair_cases(
            self._emit,
            post_pairs,
            section="SBX Assign / Startup",
            ids=self._case_ids,
            # AssignDynamicBusId only required when commissioning ran this run.
            pass_if_missing=(
                set()
                if self.params.run_commission
                else {"AssignDynamicBusId + Ack"}
            ),
        )
        self._phase_delay()

        # Soft SBX readiness after commission (charge_rate may now appear).
        try:
            self._await_sbx_ready(timeout_sec=45.0, hard_fail=False)
        except DashboardError as exc:
            self._emit(
                "sbx_ready_soft_fail",
                error=str(exc),
                message=f"SBX charge_rate not yet live (continuing): {exc}",
            )
        # Definitive capture re-check — frames often arrive only after commission.
        self._emit_sbx_capture_recheck(label="after commission / Assign-Startup")

        avail = check_availability(self.config)
        self._emit("availability", **avail.to_dict(), message="Availability recheck for load banks")
        self._validate(
            "ev_available",
            ok=bool(avail.ev_available),
            message=(
                "EV Simulator / Load Bank 1 available"
                if avail.ev_available
                else (
                    "EV Simulator is in use by "
                    f"{avail.ev.get('operator') or 'another user'}"
                )
            ),
            expected="available",
            actual="busy" if not avail.ev_available else "available",
            hard_fail=True,
        )
        if self.params.use_lb2:
            self._validate(
                "lb2_available",
                ok=bool(avail.lb2_available),
                message=(
                    "Load Bank-2 available"
                    if avail.lb2_available
                    else "Load Bank-2 is not available"
                ),
                expected="available",
                actual="busy" if not avail.lb2_available else "available",
                hard_fail=True,
            )

        # Phase 2 — Boot
        self._set_pipeline("boot")
        self._set_phase("login")
        self._emit("login_start", message="Logging in to EV Simulator (Load Bank 1)")
        ev_login = self.ev.login()
        self._ev_logged_in = True
        self._emit("login_ev", ok=ev_login.get("ok", True), message="EV Simulator login")

        if self.params.use_lb2:
            assert self.lb2 is not None
            self._emit("login_start_lb2", message="Logging in to Load Bank-2")
            lb2_login = self.lb2.login()
            self._lb2_logged_in = True
            self._emit("login_lb2", ok=lb2_login.get("ok", True), message="Load Bank-2 login")

        self._set_phase("startup")
        ev_ready = self.ev.wait_ready()
        self._emit(
            "ev_ready",
            startup_session_ready=ev_ready.get("startup_session_ready"),
            message="Load Bank 1 and EV load online — startup done",
        )

        if self.params.use_lb2:
            assert self.lb2 is not None
            lb2_ready = self.lb2.wait_ready()
            self._emit(
                "lb2_ready",
                startup_ready=lb2_ready.get("startup_ready"),
                message="Load Bank-2 ready",
            )

        self._phase_delay()

        # Phase 3 — Handshake cyclic verification (120s, soft).
        self._set_pipeline(
            "handshake",
            message="Handshake verification — monitoring cyclic SBX messages for 120s",
        )
        from drive_automation.sbx_verify import HANDSHAKE_CYCLIC_PAIRS

        handshake_pairs = watch_pairs(
            self._capture_session_dir,
            HANDSHAKE_CYCLIC_PAIRS,
            window_sec=120.0,
            stop_check=self._stop_check,
            emit=self._emit,
            event_name="handshake_verify",
        )
        emit_sbx_pair_cases(
            self._emit,
            handshake_pairs,
            section="SBX Handshake",
            ids=self._case_ids,
            # No MsgFaultCount traffic means no fault — that is PASS.
            pass_if_missing={"MsgFaultCount"},
        )
        charge_rate = self._read_charge_rate()
        self._emit(
            "panel_status",
            charge_rate_a=charge_rate,
            source="sbx",
            message=f"SBX charge_rate sample: {charge_rate}",
        )

        self.telemetry.bind(self.ev, self.lb2, extra=self._extra)
        if self._custom_emit is not None:
            custom = self._custom_emit

            def _bridge(rec: dict[str, Any]) -> None:
                ev_name = str(rec.get("event", "telemetry"))
                # Keep ts / ts_local / tz so wall-local stamps survive.
                payload = {k: v for k, v in rec.items() if k != "event"}
                custom(ev_name, **payload)

            self.telemetry.bind(self.ev, self.lb2, extra=self._extra, emit_fn=_bridge)

        self.telemetry.start()

        self._phase_delay()

        # Phase 4 — panel ratings R/W + live feeder amps.
        self._run_panel_ratings_phase()
        self._phase_delay()

        self._set_phase("test_new")
        test_name = self.params.test_name
        notes = self.params.notes
        try:
            test_resp = self.ev.test_new(
                self.params.charger_label,
                test_name,
                notes,
            )
        except DashboardError as exc:
            # Folder already exists — auto-suffix with timestamp and retry once.
            if "already exists" not in str(exc).lower():
                raise
            stamp = now_local(self.config.drive_timezone).strftime("%Y%m%d_%H%M%S")
            test_name = f"{self.params.test_name}_{stamp}"
            self._emit(
                "test_name_adjusted",
                original=self.params.test_name,
                test_name=test_name,
                message=f"Test folder exists — using {test_name}",
            )
            test_resp = self.ev.test_new(
                self.params.charger_label,
                test_name,
                notes,
            )
        self._test_name_used = test_name
        self._emit(
            "test_new",
            path=test_resp.get("path"),
            charger=self.params.charger_label,
            test_name=test_name,
            message="EV test recording started",
        )

        for iteration in range(1, self.params.iterations + 1):
            if self._stop_check():
                self._emit("run_cancelled", iteration=iteration, message="Run cancelled")
                break
            self._run_iteration(iteration)

        self._set_phase("test_save")
        try:
            save_resp = self.ev.test_save()
            self._emit(
                "test_save",
                path=save_resp.get("path"),
                message="EV test saved",
            )
        except DashboardError as exc:
            self._emit(
                "test_save_skipped",
                error=str(exc),
                message=f"Test save skipped: {exc}",
            )

        self._emit("run_complete", message="All iterations complete")

    def _export_ev_test_zip(self) -> None:
        """Download EV ``.evtest.zip`` before logout/safe exit."""
        if self.ev is None or not self.ev.logged_in:
            return
        self._set_phase("ev_export")
        self._emit("artifact_collect_start", message="Exporting EV test archive")
        try:
            self._ev_zip_bytes, self._ev_zip_name = self.ev.test_export()
            self._emit(
                "ev_test_exported",
                filename=self._ev_zip_name,
                bytes=len(self._ev_zip_bytes),
                message=(
                    f"EV test export downloaded ({len(self._ev_zip_bytes)} bytes)"
                ),
            )
        except DashboardError as exc:
            self._emit(
                "ev_test_export_skipped",
                error=str(exc),
                message=f"EV test export skipped: {exc}",
            )

    def _package_artifacts(self) -> None:
        """Bundle EV zip, run/pilot JSONL, and Modbus/SBX log for download."""
        if self._log_path is None and not self._ev_zip_bytes:
            return

        self._set_phase("artifacts")
        if self._log_path is not None:
            run_id = self._log_path.stem  # run_YYYYMMDD_HHMMSS
        else:
            run_id = now_local(self.config.drive_timezone).strftime("run_%Y%m%d_%H%M%S")

        modbus_path = None
        try:
            from drive_automation.sbx_verify import resolve_modbus_log

            modbus_path = resolve_modbus_log(self._capture_session_dir)
        except Exception:
            modbus_path = None

        try:
            self.config.artifacts_dir.mkdir(parents=True, exist_ok=True)
            artifact = build_run_artifact(
                artifacts_root=self.config.artifacts_dir,
                run_id=run_id,
                log_path=self._log_path,
                ev_zip_bytes=self._ev_zip_bytes,
                ev_zip_name=self._ev_zip_name,
                pilot_log_path=self._pilot_log_path,
                modbus_log_path=modbus_path,
                ev_loads_log_path=(
                    self._lab_capture.paths.get("ev_loads") if self._lab_capture else None
                ),
                lb2_log_path=(
                    self._lab_capture.paths.get("lb2") if self._lab_capture else None
                ),
                panel_log_path=(
                    self._lab_capture.paths.get("panel") if self._lab_capture else None
                ),
                meta={
                    "charger_label": self.params.charger_label,
                    "test_name": self._test_name_used,
                    "operator": self.config.operator,
                    "use_lb2": self.params.use_lb2,
                    "ev_url": self.config.ev_url,
                    "lb2_url": self.config.lb2_url if self.params.use_lb2 else None,
                    "breaker_rating_a": self.params.breaker_rating_a,
                    "site_capacity_a": self._site_capacity_a,
                    "last_commission": self.last_commission,
                    "drive_timezone": self.config.drive_timezone,
                    "capture_session_dir": (
                        str(self._capture_session_dir)
                        if self._capture_session_dir
                        else None
                    ),
                },
            )
            self.last_artifact = artifact
            modbus_log_url = artifact.get("modbus_url") or self._modbus_log_download_url()
            pilot_log_url = artifact.get("pilot_url")
            self._emit(
                "artifact_ready",
                run_id=artifact["run_id"],
                download_url=artifact["download_url"],
                report_url=artifact.get("report_url"),
                modbus_log_url=modbus_log_url,
                pilot_log_url=pilot_log_url,
                ev_loads_log_url=artifact.get("ev_loads_url"),
                lb2_log_url=artifact.get("lb2_url"),
                panel_log_url=artifact.get("panel_url"),
                combined_name=artifact["combined_name"],
                files=artifact["files"],
                ev_zip=artifact.get("ev_zip"),
                run_log=artifact.get("run_log"),
                pilot_log=artifact.get("pilot_log"),
                ev_loads_log=artifact.get("ev_loads_log"),
                lb2_log=artifact.get("lb2_log"),
                panel_log=artifact.get("panel_log"),
                modbus_log=artifact.get("modbus_log"),
                report=artifact.get("report"),
                dir=artifact["dir"],
                sbx_abnormality_count=len(self._sbx_abnormality_hits),
                message=(
                    "Artifacts ready — report, Modbus/SBX, Pilot, EV loads, LB2, Panel logs"
                    + (
                        f" ({len(self._sbx_abnormality_hits)} SBX abnormalities)"
                        if self._sbx_abnormality_hits
                        else ""
                    )
                ),
            )
            if artifact.get("report_url"):
                self._emit(
                    "report_ready",
                    run_id=artifact["run_id"],
                    report_url=artifact["report_url"],
                    modbus_log_url=modbus_log_url,
                    pilot_log_url=pilot_log_url,
                    ev_loads_log_url=artifact.get("ev_loads_url"),
                    lb2_log_url=artifact.get("lb2_url"),
                    panel_log_url=artifact.get("panel_url"),
                    download_url=artifact["download_url"],
                    combined_name=artifact["combined_name"],
                    message=f"Run report ready: {artifact['report_url']}",
                )
        except Exception as exc:
            self._emit(
                "artifact_error",
                error=str(exc),
                message=f"Artifact packaging failed: {exc}",
            )

    def _pilot_retry_emit(
        self,
        attempt: int,
        attempts: int,
        action: str,
        error: str,
    ) -> None:
        self._emit(
            "pilot_retry",
            attempt=attempt,
            attempts=attempts,
            action=action,
            error=error,
            message=(
                f"Pilot '{action}' failed (attempt {attempt}/{attempts}); "
                f"retrying in 10 s"
            ),
        )

    def _set_pilot_mode(self, mode: str, iteration: int) -> None:
        assert self.ev is not None
        self.ev.set_mode_with_retry(
            mode,
            attempts=3,
            delay_sec=10.0,
            on_retry=self._pilot_retry_emit,
        )
        self._emit("pilot_mode", mode=mode, iteration=iteration)

    def _run_iteration(self, iteration: int) -> None:
        assert self.ev is not None
        self._throttle_verified = False

        # Phase 4a ratings already applied; keep site capacity if known.
        if self._site_capacity_a is None:
            self._read_panel_capacity()

        self._set_phase("pilot_watch", iteration)
        self._set_pilot_mode("watch", iteration)

        self._set_phase("pilot_connect", iteration)
        self._set_pilot_mode("connect", iteration)

        self._set_phase("pilot_wait_pwm", iteration)
        pwm_reading = self.ev.wait_pwm(
            on_retry=self._pilot_retry_emit,
        )
        self._emit(
            "pilot_pwm",
            iteration=iteration,
            **self._pilot_fields(pwm_reading),
            message="PWM frequency received",
        )

        time.sleep(1.0)

        self._set_phase("pilot_charge", iteration)
        self._set_pilot_mode("charge", iteration)
        charge_reading = self.ev.pilot_with_retry(
            attempts=3,
            delay_sec=10.0,
            on_retry=self._pilot_retry_emit,
        )
        self._emit(
            "pilot_charge",
            iteration=iteration,
            **self._pilot_fields(charge_reading),
            message="Charge mode — pilot sample",
        )

        # Phase 4b — EV draw: Charge alone does not arm loads on new EV builds.
        # Switch ON Loads (POST /api/ev/loads) starts drawing at the commissioned
        # fraction (default 80%). Panel throttle then follows via pilot duty.
        commissioned_drive_a = float(self.params.breaker_rating_a)
        ev_draw = int(round(self.params.ev_draw_fraction * commissioned_drive_a))
        self._ev_draw_cap_a = ev_draw
        self._ev_retarget_amps = ev_draw
        self._set_phase("ev_switch_loads_on", iteration)
        try:
            loads_on = self.ev.switch_loads(
                "on", amps=ev_draw, approach="undershoot"
            )
        except DashboardError as exc:
            # Older EV without /api/ev/loads — fall back to max-current cap only.
            if "HTTP 404" in str(exc):
                self._emit(
                    "ev_loads_api_missing",
                    error=str(exc),
                    message=(
                        "POST /api/ev/loads not available — falling back to "
                        "max-current (upgrade EV Simulator on LB1 Pi)"
                    ),
                )
                loads_on = self.ev.set_max_current_cap(ev_draw)
            else:
                raise
        time.sleep(1.0)
        snap = self.ev.pilot_snapshot()
        armed = None
        try:
            armed = self.ev.loads_armed()
        except Exception:
            armed = None
        self._emit(
            "ev_charging",
            iteration=iteration,
            ev_max_amps=ev_draw,
            ev_draw_a=ev_draw,
            commissioned_drive_a=commissioned_drive_a,
            ev_draw_fraction=self.params.ev_draw_fraction,
            site_capacity_a=self._site_capacity_a,
            loads_armed=armed,
            loads_on=loads_on if isinstance(loads_on, dict) else None,
            **snap,
            message=(
                f"Switch ON Loads at {ev_draw} A "
                f"({int(self.params.ev_draw_fraction * 100)}% of {commissioned_drive_a:.0f} A drive)"
            ),
        )
        # Seed Drive-setpoint UI early (do not wait until mid-throttle samples).
        cr_seed = self._read_charge_rate()
        if cr_seed is None:
            # Brief settle — MsgDriveStatus / gateway mirror often lags Switch ON.
            for _ in range(15):
                if self._stop_check():
                    break
                time.sleep(1.0)
                cr_seed = self._read_charge_rate()
                if cr_seed is not None:
                    break
        if cr_seed is not None:
            self._emit(
                "drive_setpoint",
                iteration=iteration,
                charge_rate_a=cr_seed,
                charge_rate_source="sbx",
                message=f"Drive setpoint live: {cr_seed} A (panel→Drive)",
            )

        self._set_pipeline("charge", iteration=iteration)

        if self.params.use_lb2:
            delay = float(self.params.lb2_start_delay_sec)
            self._set_phase("lb2_delay", iteration, duration_sec=delay)
            self._emit(
                "lb2_delay",
                seconds=delay,
                message=f"Waiting {delay}s before LB2",
            )
            self._sleep_interruptible(delay)
            self._lb2_setup_voltages(iteration)
            if self.params.use_dynamic_load:
                self._lb2_dynamic_throttle(iteration)
            else:
                self._lb2_ramp_and_hold(iteration)

        # Must Switch Off Loads before Connect/Watch (else HTTP 409).
        self._set_phase("ev_switch_loads_off", iteration)
        try:
            loads_off = self.ev.ensure_loads_off()
            self._emit(
                "ev_loads_off",
                iteration=iteration,
                result=loads_off if isinstance(loads_off, dict) else None,
                message="Switch Off Loads complete",
            )
        except DashboardError as exc:
            if "HTTP 404" in str(exc):
                self._emit(
                    "ev_loads_api_missing",
                    error=str(exc),
                    message="POST /api/ev/loads not available during Switch Off — continuing",
                )
            else:
                raise

        self._set_phase("pilot_shutdown", iteration)
        self._set_pilot_mode("connect", iteration)
        time.sleep(1.0)
        self._set_pilot_mode("watch", iteration)
        time.sleep(1.0)
        self._emit(
            "pilot_modes_done",
            iteration=iteration,
            message="Pilot returned to Watch",
        )

    def _lb2_setup_voltages(self, iteration: int) -> None:
        assert self.lb2 is not None
        voltages = normalize_channel_voltages(self.params.lb2_channel_voltages)
        self.params.lb2_channel_voltages = voltages
        channels = tuple(self.params.panel_channels) or (1, 2, 3, 4)
        self.params.panel_channels = channels
        self._set_phase("lb2_voltages", iteration)
        # Configure voltage only on selected channels; clear loads on unused ones
        # so leftover steps do not add phantom panel current.
        for ch in (1, 2, 3, 4):
            if ch in channels:
                volt = voltages.get(ch, "240")
                self.lb2.set_voltage(ch, volt)
            else:
                try:
                    self.lb2.apply_channel_loads(ch, set())
                except Exception:
                    pass
        self.params.lb2_channel_voltages = voltages
        snap = self.lb2.meter_snapshot()
        applied = snap.get("voltages") or {}
        self._emit(
            "lb2_voltages",
            iteration=iteration,
            voltages=applied,
            requested={ch: voltages.get(ch) for ch in channels},
            panel_channels=list(channels),
            message=(
                f"LB2 channels {list(channels)} configured "
                f"({', '.join(f'CH{ch}={voltages.get(ch)}V' for ch in channels)})"
                + (f" · hardware {applied}" if applied else "")
            ),
        )

    def _resolve_breaker_instance_id(self) -> Optional[int]:
        """DER cb instance for advertise cases: commission result → params → config."""
        for raw in (
            (self.last_commission or {}).get("breaker_instance_id"),
            self.params.breaker_instance_id,
            self.config.breaker_instance_id,
        ):
            if raw is None or raw == "":
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
        return None

    def _run_jama_procedure(self) -> None:
        """EV + gateway only: snapshot → selected JAMA cases → always restore."""
        assert self.ev is not None
        if self.gateway is None or not self.gateway.available:
            raise DashboardError(
                "JAMA test pipeline requires gateway SSH "
                "(GATEWAY_SERIAL / spanssh-config or GATEWAY_SSH_HOST)."
            )

        snap = None
        try:
            self._set_pipeline(
                "capture",
                message="Starting local SBX/UART capture for JAMA test",
            )
            self._start_local_capture(require_charge_rate=False)
            self._start_sbx_abnormality_watch()
            self._emit_capture_stream_status()

            self._set_pipeline("boot", message="JAMA test — EV Simulator login")
            self._set_phase("login")
            self._emit("login_start", message="Logging in to EV Simulator (Load Bank 1)")
            ev_login = self.ev.login()
            self._ev_logged_in = True
            self._emit("login_ev", ok=ev_login.get("ok", True), message="EV Simulator login")

            self._set_phase("startup")
            ev_ready = self.ev.wait_ready()
            self._emit(
                "ev_ready",
                startup_session_ready=ev_ready.get("startup_session_ready"),
                message="EV Simulator ready",
            )
            self.telemetry.start()
            try:
                from drive_automation.advertise_cases import ensure_pilot_charge

                ensure_pilot_charge(self.ev)
                self._emit(
                    "pilot_charge",
                    message="Pilot Charge armed for JAMA / SBX comms check",
                )
            except Exception as exc:
                self._emit(
                    "pilot_charge",
                    ok=False,
                    error=str(exc),
                    message=f"Pilot Charge not armed (continuing): {exc}",
                )

            self._set_pipeline(
                "handshake",
                message="SBX comms verification — cyclic Modbus messages",
            )
            self._set_phase("handshake")
            from drive_automation.sbx_verify import HANDSHAKE_CYCLIC_PAIRS, watch_pairs

            handshake_pairs = watch_pairs(
                self._capture_session_dir,
                HANDSHAKE_CYCLIC_PAIRS,
                window_sec=45.0,
                stop_check=self._stop_check,
                emit=self._emit,
                event_name="handshake_verify",
            )
            emit_sbx_pair_cases(
                self._emit,
                handshake_pairs,
                section="SBX Handshake",
                ids=self._case_ids,
                pass_if_missing={"MsgFaultCount"},
            )

            instance_id = self._resolve_breaker_instance_id()
            if instance_id is None:
                raise DashboardError(
                    "JAMA test requires DER breaker instance id — set "
                    "DRIVE_BREAKER_INSTANCE_ID in config.env (or pass breaker_instance_id)."
                )

            self._set_pipeline(
                "snapshot",
                message="Snapshot mutable state (user-max, seriald, pilot, relay)",
            )
            self._set_phase("jama_snapshot")
            snap = snapshot_jama_state(
                self.gateway,
                self.ev,
                breaker_instance_id=instance_id,
            )
            self._emit(
                "jama_snapshot",
                user_max_a=snap.user_max_a,
                seriald_active=snap.seriald_active,
                pilot_mode=snap.pilot_mode,
                breaker_instance_id=snap.breaker_instance_id,
                message=(
                    f"Noted start state: user_max={snap.user_max_a} A, "
                    f"seriald={'active' if snap.seriald_active else 'inactive'}, "
                    f"pilot={snap.pilot_mode}, cb={snap.breaker_instance_id}"
                ),
            )

            selected = None
            if self.params.jama_cases:
                selected = frozenset(str(c) for c in self.params.jama_cases)

            self._set_pipeline("jama_test", message="Running selected JAMA cases")
            self._set_phase("jama_test")
            summary = run_selected_jama_cases(
                self.gateway,
                self.ev,
                self._emit,
                instance_id=instance_id,
                case_ids=self._case_ids,
                selected=selected,
            )
            self._emit(
                "jama_test_done",
                ok=bool(summary.get("ok")),
                summary=summary,
                message=(
                    "JAMA cases " + ("PASS" if summary.get("ok") else "FAIL")
                ),
            )
            if not summary.get("ok"):
                failed = [
                    k
                    for k, v in summary.items()
                    if k not in ("selected", "ok")
                    and isinstance(v, dict)
                    and not v.get("ok")
                ]
                raise DashboardError(
                    "JAMA cases failed: " + (", ".join(failed) if failed else "see log")
                )
        finally:
            # Always revert to values noted at pipeline start.
            if snap is not None and self.gateway is not None:
                self._set_pipeline(
                    "restore",
                    message="Restore snapshot (user-max, seriald, pilot, relay)",
                )
                self._set_phase("jama_restore")
                try:
                    restore_jama_state(self.gateway, self.ev, snap, self._emit)
                except Exception as exc:
                    self._emit(
                        "jama_restore_error",
                        error=str(exc),
                        message=f"JAMA restore failed: {exc}",
                    )

    def _run_commission_phase(self) -> None:
        self._set_pipeline("commission")
        self._set_phase("commission")
        if not self.config.use_gateway:
            raise DashboardError(
                "run_commission requires gateway access — set USE_GATEWAY=1 "
                "and GATEWAY_SERIAL + spanssh-config in config.env."
            )
        if self.gateway is None or not self.gateway.available:
            raise DashboardError(
                "run_commission requested but gateway SSH (spanssh) is not configured."
            )
        serial = (self.params.drive_serial or self.config.drive_serial or "").strip()
        if not serial:
            raise DashboardError("DRIVE_SERIAL is required when run_commission is enabled.")
        top_pos = (
            self.params.top_position
            if self.params.top_position is not None
            else (self.params.drive_top_tab or self.config.drive_top_tab)
        )
        bottom_pos = (
            self.params.bottom_position
            if self.params.bottom_position is not None
            else (self.params.drive_bottom_tab or self.config.drive_bottom_tab)
        )
        if top_pos is None or bottom_pos is None:
            raise DashboardError(
                "Breaker top/bottom positions are required for commissioning."
            )
        if not self.params.breaker_rating_a:
            raise DashboardError("Breaker rating (A) is required for commissioning.")
        result = commission_drive(
            self.gateway,
            CommissionParams(
                drive_serial=serial,
                top_position=int(top_pos),
                bottom_position=int(bottom_pos),
                breaker_rating_ma=int(self.params.breaker_rating_a * 1000),
                timezone=self.params.drive_timezone or self.config.drive_timezone,
            ),
            emit=self._emit,
        )
        self.last_commission = {
            "breaker_instance_id": result.breaker_instance_id,
            "top_bc_instance": result.top_bc_instance,
            "bottom_bc_instance": result.bottom_bc_instance,
            "top_tab_number": result.top_tab_number,
            "bottom_tab_number": (
                int(bottom_pos) if bottom_pos is not None else None
            ),
            "hardware_id": result.hardware_id,
            "branch_id": result.branch_id,
            "discovery_state": result.discovery_state,
            "breaker_rating_a": float(self.params.breaker_rating_a),
            "details": dict(result.details or {}),
            "message": result.message,
            "ok": bool(result.ok),
        }
        if result.breaker_instance_id is not None:
            try:
                self.params.breaker_instance_id = int(result.breaker_instance_id)
            except (TypeError, ValueError):
                pass

    def _lb2_dynamic_throttle(self, iteration: int) -> None:
        """Phases 5–8: closed-loop load following panel charge_rate then pilot adv."""
        assert self.lb2 is not None
        assert self.ev is not None

        ctrl_params = LoadControllerParams(
            breaker_rating_a=float(
                self.params.breaker_rating_a or self.config.breaker_rating_a
            ),
            max_lb2_amps=max(0.0, float(self.params.lb2_max_amps) or 40.0),
            loop_interval_sec=float(
                self.params.load_loop_interval_sec or self.config.load_loop_interval_sec
            ),
            deadband_a=float(self.params.load_deadband_a or self.config.load_deadband_a),
            slew_a_per_s=float(self.config.slew_a_per_s),
            panel_channels=self.params.panel_channels,
            channel_voltages=self.params.lb2_channel_voltages,
            aggregation_timeout_sec=float(
                self.params.aggregation_timeout_sec
                or self.config.aggregation_timeout_sec
            ),
            sustain_timeout_sec=float(
                self.params.sustain_timeout_sec
                or self.params.throttle_duration_sec
                or self.config.sustain_timeout_sec
            ),
            recovery_timeout_sec=float(
                self.params.recovery_duration_sec or self.config.recovery_timeout_sec
            ),
            aggregation_step_a=max(0.5, float(self.params.lb2_step_a or 5.0)),
            full_throttle_duty_pct=float(self.params.full_throttle_duty_pct or 10.0),
            metrics_settle_sec=2.5,
            observed_wait_sec=8.0,
            balance_tolerance_a=3.0,
            duty_follow_timeout_sec=float(
                getattr(self.params, "duty_follow_timeout_sec", 15.0) or 15.0
            ),
            duty_follow_tolerance_a=float(
                getattr(self.params, "duty_follow_tolerance_a", 3.0) or 3.0
            ),
            duty_follow_poll_sec=0.5,
        )
        controller = LoadController(
            self.lb2,
            ctrl_params,
            emit=self._emit,
            stop_check=self._stop_check,
        )

        # charge_rate (panel->drive commanded current) comes from the live SBX log.
        def read_charge_rate() -> Optional[float]:
            return self._read_charge_rate()

        def read_charge_rate_detail() -> Optional[dict[str, Any]]:
            return self._read_charge_rate_detail()

        def read_panel_total_a() -> Optional[float]:
            """Cached feeder combined A from background watcher."""
            watcher = self._feeder_watcher
            if watcher is not None:
                sample = getattr(watcher, "last_sample", None) or {}
                combined = sample.get("combined_a")
                if combined is not None:
                    try:
                        return float(combined)
                    except (TypeError, ValueError):
                        pass
            return None

        def refresh_panel_total_a() -> Optional[float]:
            """Fresh traits-cli feeder sample (slower SSH) for JAMA balance checks."""
            if self.gateway is None:
                return read_panel_total_a()
            from drive_automation.panel_ratings import sample_feeder_amps

            try:
                sample = sample_feeder_amps(
                    self.gateway,
                    timeout_s=8.0,
                    emit=self._emit,
                    quiet=True,
                )
            except Exception:
                sample = None
            if sample and sample.get("combined_a") is not None:
                watcher = self._feeder_watcher
                if watcher is not None:
                    watcher.last_sample = sample
                try:
                    return float(sample["combined_a"])
                except (TypeError, ValueError):
                    pass
            return read_panel_total_a()

        def read_duty_cycle_pct() -> Optional[float]:
            assert self.ev is not None
            try:
                pilot = self.ev.pilot_with_retry(
                    attempts=3,
                    delay_sec=1.0,
                    timeout=8.0,
                    on_retry=self._pilot_retry_emit,
                )
                duty = pilot.get("duty_cycle_pct")
                duty_f = float(duty) if duty is not None else None
                adv_raw = pilot.get("max_current_a")
                try:
                    adv = float(adv_raw) if adv_raw is not None else None
                except (TypeError, ValueError):
                    adv = None
                implied = None
                if duty_f is not None and 10.0 <= duty_f <= 85.0:
                    implied = round(duty_f * 0.6, 2)
                # Prefer pilot-advertised amps; fall back to IEC duty→amps.
                target_a: Optional[float] = None
                if adv is not None and adv >= 0:
                    target_a = adv
                elif implied is not None:
                    target_a = implied
                applied = None
                # Snap EV contactors to the new duty amp (clean undershoot step).
                if target_a is not None and self._ev_draw_cap_a > 0:
                    step_amps = int(round(min(float(target_a), float(self._ev_draw_cap_a))))
                    step_amps = max(0, step_amps)
                    if (
                        self._ev_retarget_amps is None
                        or abs(step_amps - int(self._ev_retarget_amps)) >= 1
                    ):
                        try:
                            self.ev.retarget_loads(step_amps, approach="undershoot")
                            self._ev_retarget_amps = step_amps
                            self._emit(
                                "ev_loads_retarget",
                                amps=step_amps,
                                duty_cycle_pct=duty_f,
                                duty_implied_a=implied,
                                pilot_max_current_a=adv,
                                message=(
                                    f"EV contactors stepped to {step_amps} A "
                                    f"(duty {duty_f}% undershoot)"
                                ),
                            )
                            try:
                                st = self.ev.state() or {}
                                applied = st.get("applied_current_a")
                            except Exception:
                                applied = None
                        except DashboardError as retarget_exc:
                            self._emit(
                                "ev_loads_retarget",
                                ok=False,
                                amps=step_amps,
                                error=str(retarget_exc)[:200],
                                message=f"EV retarget failed: {retarget_exc}",
                            )
                self._emit(
                    "pilot_duty_sample",
                    duty_cycle_pct=duty_f,
                    pilot_max_current_a=adv,
                    duty_implied_a=implied,
                    frequency_hz=pilot.get("frequency_hz"),
                    applied_current_a=applied,
                    ev_retarget_amps=self._ev_retarget_amps,
                    message=(
                        f"EV pilot duty={duty_f}"
                        + (f"% (~{implied} A)" if implied is not None else "%")
                        + (
                            f" retarget={self._ev_retarget_amps} A"
                            if self._ev_retarget_amps is not None
                            else ""
                        )
                    ),
                )
                return duty_f
            except Exception as exc:
                self._emit(
                    "pilot_duty_sample",
                    ok=False,
                    error=str(exc)[:200],
                    message=f"EV pilot read failed: {exc}",
                )
                return None

        step_delay = float(
            self.params.lb2_ramp_step_interval_sec
            or self.config.load_loop_interval_sec
            or 5.0
        )

        # Minimize background EV HTTP during load control (aggregation samples
        # drive duty/applied; concurrent telemetry caused pilot read timeouts).
        self.telemetry.lighten_for_load_control()
        self._emit(
            "telemetry_lightened",
            interval_sec=30.0,
            poll_ev=False,
            message="Telemetry lightened for aggregate/throttle/recover (EV poll off)",
        )
        try:
            # --- Phase 5: Aggregate (step LB2 up until SBX charge_rate -> 6 A -> 0 A) ---
            self._set_pipeline("aggregate", iteration=iteration)
            # No fixed duration — aggregation runs until throttle goal / ceiling decision.
            self._set_phase("aggregate", iteration)
            agg = controller.run_aggregation_sbx(
                iteration=iteration,
                read_charge_rate=read_charge_rate,
                step_delay_sec=step_delay,
                site_capacity_a=self._site_capacity_a,
                read_duty_cycle_pct=read_duty_cycle_pct,
                request_ceiling_override=self._request_ceiling_override,
                read_charge_rate_detail=read_charge_rate_detail,
                read_panel_total_a=read_panel_total_a,
                refresh_panel_total_a=refresh_panel_total_a,
                case_ids=self._case_ids,
            )
            self._commanded_lb2_amps = controller.commanded_amps
            if not agg.get("ok"):
                reason = str(agg.get("reason") or "aggregation_failed")
                if reason == "no_sbx_charge_rate":
                    self._validate(
                        "aggregation_sbx_charge_rate",
                        ok=False,
                        message=(
                            "No SBX charge_rate while aggregating — load control cannot "
                            "read panel→drive commanded current"
                        ),
                        expected="charge_rate_a present",
                        actual="null",
                        detail=agg,
                        hard_fail=True,
                    )
                if reason == "cancelled":
                    raise DashboardError("Aggregation cancelled")
                # Throttle engagement is NOT a hard requirement: whether the panel
                # drops charge_rate depends on commissioned breaker rating, site
                # capacity, and how hard LB2 is pushed — not a fixed pass/fail gate.
                self._validate(
                    "aggregation_throttle_engaged",
                    ok=False,
                    message=(
                        "No panel throttle observed while LB2 was stepped up "
                        f"(reason={reason}, LB2={agg.get('commanded_amps')} A, "
                        f"baseline charge_rate={agg.get('baseline_charge_rate_a')} A). "
                        "Continuing — throttle depends on panel/Drive commissioning, "
                        "not required for this run to pass."
                    ),
                    expected="optional: charge_rate drop vs baseline",
                    actual={
                        "baseline_charge_rate_a": agg.get("baseline_charge_rate_a"),
                        "commanded_amps": agg.get("commanded_amps"),
                        "engaged": agg.get("engaged"),
                    },
                    detail=agg,
                    hard_fail=False,
                )
            else:
                self._validate(
                    "aggregation_throttle_engaged",
                    ok=True,
                    message=(
                        "Throttle engaged — panel reduced drive charge_rate "
                        f"(baseline {agg.get('baseline_charge_rate_a')} A → "
                        f"LB2 {agg.get('commanded_amps')} A)"
                    ),
                    expected="charge_rate drop vs baseline",
                    actual={
                        "baseline_charge_rate_a": agg.get("baseline_charge_rate_a"),
                        "reached_6a": agg.get("reached_6a"),
                        "reached_0a": agg.get("reached_0a"),
                        "commanded_amps": agg.get("commanded_amps"),
                    },
                )
                self._validate(
                    "aggregation_reached_6a",
                    ok=bool(agg.get("reached_6a")),
                    message=(
                        "charge_rate reached ≤6 A"
                        if agg.get("reached_6a")
                        else (
                            "Throttle engaged but charge_rate never reached ≤6 A "
                            "(informational — not required to pass)"
                        )
                    ),
                    expected="optional: charge_rate_a ≤ 6",
                    actual={
                        "reached_6a": agg.get("reached_6a"),
                        "reached_0a": agg.get("reached_0a"),
                    },
                    detail=agg,
                    hard_fail=False,
                )

            # --- Phase 6/7: Sustain + Verify ---
            self._set_pipeline("throttle", iteration=iteration)
            # Brief settle only — no long UI countdown (timeouts removed from operator UI).
            sustain_sec = min(30.0, float(ctrl_params.sustain_timeout_sec or 30.0))
            self._set_phase("throttle", iteration)
            self._emit(
                "throttle_sustain_start",
                iteration=iteration,
                duration_sec=sustain_sec,
                message=f"Sustaining load ({sustain_sec:g} s)",
            )
            self._sleep_interruptible(sustain_sec)

            self._set_pipeline("verify", iteration=iteration)
            self._set_phase("verify", iteration)
            cr_now = read_charge_rate()
            baseline = agg.get("baseline_charge_rate_a")
            engaged = bool(agg.get("engaged"))
            if engaged:
                still_throttled = (
                    cr_now is not None
                    and baseline is not None
                    and float(cr_now) < (float(baseline) - float(ctrl_params.deadband_a))
                )
                self._validate(
                    "throttle_still_engaged",
                    ok=bool(still_throttled),
                    message=(
                        f"After sustain, charge_rate still below baseline "
                        f"({cr_now} A vs baseline {baseline} A)"
                        if still_throttled
                        else (
                            f"After sustain, charge_rate did not stay throttled "
                            f"(got {cr_now} A, baseline {baseline} A) — informational"
                        )
                    ),
                    expected=f"optional: charge_rate < baseline − {ctrl_params.deadband_a} A",
                    actual={"charge_rate_a": cr_now, "baseline_charge_rate_a": baseline},
                    hard_fail=False,
                )
                if still_throttled:
                    self._throttle_verified = True
                    self._emit(
                        "throttle_verified",
                        iteration=iteration,
                        charge_rate_a=cr_now,
                        baseline_charge_rate_a=baseline,
                        message="Throttle verified against SBX charge_rate drop",
                    )
            else:
                self._validate(
                    "throttle_still_engaged",
                    ok=True,
                    message=(
                        "Verify skipped — panel did not throttle during aggregate "
                        "(expected when LB2 load stays within commissioned headroom)"
                    ),
                    expected="N/A when throttle not engaged",
                    actual={"charge_rate_a": cr_now, "engaged": False},
                    hard_fail=False,
                )

            self._phase_delay()

            # --- Phase 8: Recover (step LB2 down; charge_rate rises back to baseline) ---
            self._set_pipeline("recover", iteration=iteration)
            self._set_phase(
                "recovery",
                iteration,
                duration_sec=ctrl_params.recovery_timeout_sec,
            )
            recovery = controller.run_recovery_sbx(
                iteration=iteration,
                read_charge_rate=read_charge_rate,
                step_delay_sec=step_delay,
                read_charge_rate_detail=read_charge_rate_detail,
                read_panel_total_a=read_panel_total_a,
                refresh_panel_total_a=refresh_panel_total_a,
                read_duty_cycle_pct=read_duty_cycle_pct,
                case_ids=self._case_ids,
            )
            self._commanded_lb2_amps = controller.commanded_amps
            if engaged:
                self._validate(
                    "recovery_charge_rate_restored",
                    ok=bool(recovery.get("ok")),
                    message=(
                        "Recovery validated — charge_rate returned toward baseline as LB2 stepped down"
                        if recovery.get("ok")
                        else (
                            "Recovery: charge_rate did not fully return toward baseline "
                            "(informational)"
                        )
                    ),
                    expected="optional: charge_rate near baseline after LB2 → 0",
                    actual={
                        "charge_rate_a": recovery.get("charge_rate_a"),
                        "commanded_amps": recovery.get("commanded_amps"),
                        "baseline_charge_rate_a": baseline,
                    },
                    detail=recovery,
                    hard_fail=False,
                )
            else:
                self._validate(
                    "recovery_charge_rate_restored",
                    ok=True,
                    message="Recovery complete (LB2 stepped down; no throttle to reverse)",
                    expected="N/A when throttle not engaged",
                    actual={
                        "charge_rate_a": recovery.get("charge_rate_a"),
                        "commanded_amps": recovery.get("commanded_amps"),
                    },
                    hard_fail=False,
                )

        finally:
            self.telemetry.restore_default_polling()
            self._emit(
                "telemetry_restored",
                message="Telemetry restored to default interval after load control",
            )

    def resolve_ceiling_override(
        self,
        *,
        allow: bool,
        new_ceiling_a: Optional[float] = None,
    ) -> dict[str, Any]:
        """Dashboard answer to a ``ceiling_override_prompt`` (controller only)."""
        if not self._ceiling_override_pending:
            return {"ok": False, "error": "No ceiling override is pending."}
        ceiling = None
        if allow:
            try:
                ceiling = float(new_ceiling_a) if new_ceiling_a is not None else None
            except (TypeError, ValueError):
                return {"ok": False, "error": "new_ceiling_a must be a number."}
            if ceiling is None or ceiling <= 0:
                return {"ok": False, "error": "new_ceiling_a must be > 0."}
        self._ceiling_override_response = {
            "allow": bool(allow),
            "new_ceiling": ceiling,
        }
        self._ceiling_override_event.set()
        return {
            "ok": True,
            "allow": bool(allow),
            "new_ceiling": ceiling,
            "message": (
                f"Ceiling raise accepted → {ceiling:g} A"
                if allow
                else "Ceiling raise declined"
            ),
        }

    def _request_ceiling_override(self, prompt: dict[str, Any]) -> Optional[float]:
        """Block until the dashboard answers the ceiling raise popup (or timeout/stop).

        Re-broadcasts ``ceiling_override_prompt`` every ~15 s so a missed SSE
        event (queue drop / reconnect) still brings the modal back — only while
        the override is actually pending.
        """
        self._ceiling_override_response = None
        self._ceiling_override_event.clear()
        self._ceiling_override_pending = True
        self._ceiling_override_prompt = dict(prompt)
        timeout = float(self.params.ceiling_override_timeout_sec or 300.0)
        deadline = time.monotonic() + timeout
        next_remind = time.monotonic() + 15.0
        cancelled = False
        try:
            while time.monotonic() < deadline:
                if self._stop_check():
                    cancelled = True
                    return None
                now = time.monotonic()
                if now >= next_remind and self._ceiling_override_pending:
                    self._emit(
                        "ceiling_override_prompt",
                        **dict(prompt),
                        reminder=True,
                        message=(
                            prompt.get("message")
                            or "Still waiting for LB2 ceiling decision — popup should be visible"
                        ),
                    )
                    next_remind = now + 15.0
                if self._ceiling_override_event.wait(0.5):
                    break
            else:
                self._emit(
                    "ceiling_override_timeout",
                    message=(
                        f"No operator response within {timeout:g} s — "
                        "ending aggregation at current LB2 ceiling"
                    ),
                    **{k: prompt.get(k) for k in (
                        "commanded_amps",
                        "max_lb2_amps",
                        "duty_cycle_pct",
                        "at_hardware_max",
                    )},
                )
                return None
        finally:
            self._ceiling_override_pending = False
            self._ceiling_override_prompt = None
            if cancelled:
                self._emit(
                    "ceiling_override_cancelled",
                    message="Ceiling override cancelled (stop / run ending)",
                    **{k: prompt.get(k) for k in (
                        "commanded_amps",
                        "max_lb2_amps",
                        "duty_cycle_pct",
                        "at_hardware_max",
                    )},
                )

        resp = self._ceiling_override_response or {}
        if not resp.get("allow"):
            return None
        try:
            return float(resp.get("new_ceiling"))
        except (TypeError, ValueError):
            return None

    def _lb2_ramp_and_hold(self, iteration: int) -> None:
        assert self.lb2 is not None
        target = max(0.0, float(self.params.lb2_max_amps))
        self._commanded_lb2_amps = 0.0
        slew = max(0.1, float(self.config.slew_a_per_s))
        ramp_up_est = target / slew if target > 0 else 0.0

        self._set_phase("lb2_ramp_up", iteration, duration_sec=ramp_up_est or None)
        self._emit(
            "lb2_ramp_start",
            iteration=iteration,
            target_amps=target,
            message=f"Ramping LB2 to {target} A",
        )
        self._ramp_lb2_amps(0.0, target, iteration)

        throttle_sec = float(self.params.throttle_duration_sec)
        self._set_phase("throttle", iteration, duration_sec=throttle_sec)
        self._emit(
            "throttle_start",
            iteration=iteration,
            duration_sec=throttle_sec,
            message="Throttle test running",
        )
        self._sleep_interruptible(throttle_sec)

        ramp_down_est = self._commanded_lb2_amps / slew if self._commanded_lb2_amps > 0 else 0.0
        self._set_phase("lb2_ramp_down", iteration, duration_sec=ramp_down_est or None)
        self._emit(
            "lb2_ramp_down_start",
            iteration=iteration,
            message="Ramping LB2 load to zero",
        )
        self._ramp_lb2_amps(self._commanded_lb2_amps, 0.0, iteration)

        recovery_sec = float(self.params.recovery_duration_sec)
        self._set_phase("recovery", iteration, duration_sec=recovery_sec)
        self._emit(
            "recovery_start",
            iteration=iteration,
            duration_sec=recovery_sec,
            message="Recovery period after load removed",
        )
        self._sleep_interruptible(recovery_sec)

    def _ramp_lb2_amps(self, start: float, end: float, iteration: int) -> None:
        assert self.lb2 is not None
        step_interval = max(0.5, float(self.params.lb2_ramp_step_interval_sec))
        step_amps = max(0.5, float(self.params.lb2_step_a or 5.0))
        current = float(start)
        next_step_at = time.monotonic()

        while abs(current - end) > 0.05:
            if self._stop_check():
                return
            now = time.monotonic()
            if now >= next_step_at:
                if end > current:
                    current = min(current + step_amps, end)
                else:
                    current = max(current - step_amps, end)

                self._commanded_lb2_amps = current
                desired = select_loads_panel_total(
                    current,
                    self.params.panel_channels,
                    channel_voltages=self.params.lb2_channel_voltages,
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
                    "lb2_ramp_step",
                    iteration=iteration,
                    commanded_amps=round(current, 2),
                    target_amps=end,
                    loads={str(ch): sorted(v) for ch, v in desired.items()},
                    message=f"LB2 commanded {current:.1f} A",
                )
                next_step_at = now + step_interval

            time.sleep(0.1)

        self._commanded_lb2_amps = end
        if end <= 0.01:
            self.lb2.apply_panel_loads(
                {ch: set() for ch in self.params.panel_channels},
                panel_channels=self.params.panel_channels,
            )

    def _safe_exit_all(self, *, ev_was_active: bool, lb2_was_active: bool) -> None:
        self._set_phase("safe_exit")
        self._emit("safe_exit_started", message="Starting safe exit")

        if self.ev and ev_was_active:
            try:
                self.ev.safe_stop()
                status = self.ev.wait_shutdown()
                self._emit("safe_exit_ev", shutdown_status=status, message="EV safe exit done")
            except Exception as exc:
                self._emit("safe_exit_ev_error", error=str(exc), message="EV safe exit failed")
            try:
                self.ev.logout()
            except Exception:
                pass

        if self.lb2 and lb2_was_active:
            try:
                self.lb2.safe_shutdown()
                status = self.lb2.wait_shutdown()
                self._emit("safe_exit_lb2", shutdown_status=status, message="LB2 safe exit done")
            except Exception as exc:
                self._emit("safe_exit_lb2_error", error=str(exc), message="LB2 safe exit failed")
            try:
                self.lb2.logout()
            except Exception:
                pass

        try:
            avail = check_availability(self.config)
            self._emit(
                "safe_exit_complete",
                ev_in_use=avail.ev_in_use,
                lb2_in_use=avail.lb2_in_use,
                message="Safe exit complete",
            )
        except Exception as exc:
            self._emit("safe_exit_complete", error=str(exc), message="Safe exit finished")

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stop_check():
                return
            time.sleep(min(0.25, end - time.monotonic()))

    def _phase_delay(self) -> None:
        """Optional pause between major pipeline stages only.

        Not used between pilot watch/connect/charge substeps, and not used as
        the LB2 aggregation sample interval (that uses ``lb2_ramp_step_interval_sec``).
        """
        delay = float(self.params.phase_delay_sec or 0.0)
        if delay <= 0:
            return
        self._emit(
            "phase_delay",
            duration_sec=delay,
            pipeline_phase=self.pipeline_phase,
            message=f"Phase timing pause {delay:g} s (major stage only)",
        )
        self._sleep_interruptible(delay)

    def _sbx_watch_context(self) -> dict[str, Any]:
        return {
            "pipeline_phase": self.pipeline_phase,
            "phase": self._phase,
            "iteration": self._iteration,
        }

    def _start_sbx_abnormality_watch(self) -> None:
        """Background poll of Modbus log for fault_mask / UNDECODED / CP reset."""
        if self._capture_session_dir is None:
            return
        self._sbx_abnormality_stop.clear()
        self._sbx_abnormality_hits.clear()
        session_dir = self._capture_session_dir

        def _worker() -> None:
            from drive_automation.sbx_verify import watch_abnormalities

            watch_abnormalities(
                session_dir,
                stop_check=self._sbx_abnormality_stop.is_set,
                emit=self._emit,
                get_context=self._sbx_watch_context,
                hits_out=self._sbx_abnormality_hits,
            )

        self._sbx_abnormality_thread = threading.Thread(
            target=_worker,
            name="sbx-abnormality",
            daemon=True,
        )
        self._sbx_abnormality_thread.start()

    def _stop_sbx_abnormality_watch(self) -> None:
        """Stop live SBX monitor and emit summary for the HTML report."""
        self._sbx_abnormality_stop.set()
        if self._sbx_abnormality_thread is not None:
            self._sbx_abnormality_thread.join(timeout=8.0)
            self._sbx_abnormality_thread = None

        from drive_automation.sbx_verify import (
            resolve_modbus_log,
            scan_modbus_log_abnormalities,
        )

        path = resolve_modbus_log(self._capture_session_dir)
        if path is not None and path.is_file():
            for hit in scan_modbus_log_abnormalities(path):
                key = (
                    f"{hit.get('kind')}|{hit.get('ts')}|{hit.get('message')}"
                )
                existing = {
                    f"{h.get('kind')}|{h.get('ts')}|{h.get('message')}"
                    for h in self._sbx_abnormality_hits
                }
                if key not in existing:
                    row = {**hit, **self._sbx_watch_context()}
                    self._sbx_abnormality_hits.append(row)
                    self._emit(
                        "sbx_abnormality",
                        **row,
                        message=row.get("message") or "SBX abnormality",
                    )

        count = len(self._sbx_abnormality_hits)
        self._emit(
            "sbx_abnormality_summary",
            count=count,
            modbus_log=str(path) if path else None,
            abnormalities=self._sbx_abnormality_hits[:50],
            message=(
                f"No SBX abnormalities detected in Modbus log"
                if count == 0
                else f"{count} SBX abnormality(ies) detected — see report"
            ),
        )
        if count > 0:
            from drive_automation.jama_cases import emit_jama_case

            first = self._sbx_abnormality_hits[0]
            evidence_rows: list[dict[str, Any]] = []
            for hit in self._sbx_abnormality_hits[:8]:
                if hit.get("evidence"):
                    evidence_rows.append(
                        {
                            "stream": "modbus",
                            "ts": hit.get("ts") or "",
                            "line": str(hit.get("evidence"))[:400],
                            "label": hit.get("kind") or "sbx_abnormality",
                        }
                    )
            emit_jama_case(
                self._emit,
                case_id=self._case_ids.next("SBX"),
                section="SBX monitor",
                title=f"SBX stream clean during run ({count} abnormality(ies))",
                signal="fault_mask / UNDECODED / CP reset",
                expected="clean SBX stream (no fault_mask, UNDECODED, or fault state)",
                observed=(
                    f"{count} event(s): "
                    + "; ".join(
                        str(h.get("message") or h.get("kind") or "?")
                        for h in self._sbx_abnormality_hits[:5]
                    )
                ),
                sources="modbus",
                result="fail",
                evidence=evidence_rows,
                show_evidence=True,
                metrics={
                    "sbx_abnormality_count": count,
                    "fault_mask": first.get("fault_mask"),
                    "kind": first.get("kind"),
                },
                iteration=self._iteration,
            )
            self._validate(
                "sbx_abnormalities",
                ok=False,
                message=f"{count} SBX abnormality(ies) during run (fault / UNDECODED / reset)",
                expected="clean SBX stream",
                actual=f"{count} event(s)",
                detail=self._sbx_abnormality_hits[:8],
            )

    def _modbus_log_download_url(self) -> Optional[str]:
        """Dashboard link to the capture session Modbus log file."""
        from urllib.parse import quote

        from drive_automation.sbx_verify import resolve_modbus_log

        if self._capture_session_dir is None:
            return None
        path = resolve_modbus_log(self._capture_session_dir)
        if path is None or not path.is_file():
            return None
        session_id = self._capture_session_dir.name
        rel = path.relative_to(self._capture_session_dir).as_posix()
        return (
            f"/api/logs/unified/download?session_id={quote(session_id)}"
            f"&rel={quote(rel)}"
        )

    def _start_local_capture(self, *, require_charge_rate: bool = True) -> None:
        """Start JAMA-style local capture (SBX + UART) before Boot.

        Not through the gateway — SSHes to the local wall PC (CAPTURE_SSH_*),
        same as the lucid_automation one-click capture.

        CAN bus is masked by default until PCAN is available on this wall
        (``DRIVE_MASK_CANBUS=0`` to re-enable).

        When ``require_charge_rate`` is False (capture-before-commission), do not
        hard-fail if the Drive is decommissioned and SBX is silent yet.
        """
        import capture_gateway_config as capgw
        import dashboard_logs as dlog

        creds = capgw.resolve_capture_gateway(capgw.LOCAL_CAPTURE_ID)
        if not creds.get("ok"):
            raise DashboardError(
                "Cannot start log capture — set CAPTURE_SSH_HOST / CAPTURE_SSH_USER "
                f"and PANEL_SSH_PASS in config.env ({creds.get('error') or 'no local capture target'})."
            )
        user = str(creds["username"])
        host = str(creds["host"])
        pw = str(creds.get("password") or "")

        # Temporary: one-click does not require CAN (no PCAN on some walls).
        mask_can = (os.environ.get("DRIVE_MASK_CANBUS") or "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        unified = dlog.start_unified(
            username=user,
            host=host,
            password=pw,
            skip_can=mask_can,
        )
        if not unified.get("ok"):
            self._validate(
                "sbx_capture_start",
                ok=False,
                message=f"SBX capture failed to start: {unified.get('error')}",
                expected="unified capture running",
                actual=unified.get("error") or "start failed",
                hard_fail=True,
            )
        self._capture_session_dir = dlog.active_unified_session_dir()
        try:
            health = dlog.unified_status()
        except Exception as exc:
            health = {"error": str(exc)}
        capture_label = "SBX only (CAN masked)" if mask_can else "SBX+CAN"
        self._emit(
            "capture_started",
            session_dir=str(self._capture_session_dir or ""),
            host=host,
            username=user,
            can_masked=mask_can,
            message=f"Local capture started ({capture_label}) on {user}@{host}",
        )
        self._validate(
            "sbx_capture_start",
            ok=True,
            message=f"Log capture started ({capture_label}) — same Modbus/SBX stream as manual",
            actual={"host": host, "session_dir": str(self._capture_session_dir or "")},
        )

        uart = dlog.start_uart_capture(username=user, host=host, password=pw)
        if uart.get("ok"):
            self._emit("uart_capture_started", message="UART capture started")
            self._validate(
                "uart_capture_start",
                ok=True,
                message="UART capture started",
            )
        else:
            # UART is best-effort; SBX is the one the pipeline depends on.
            self._emit(
                "uart_capture_skipped",
                error=uart.get("error"),
                message=f"UART capture skipped: {uart.get('error')}",
            )
            self._validate(
                "uart_capture_start",
                ok=False,
                message=f"UART capture skipped (non-fatal): {uart.get('error')}",
                actual=uart.get("error"),
                hard_fail=False,
            )

        if require_charge_rate:
            self._await_sbx_ready(timeout_sec=45.0, hard_fail=True)
        else:
            self._emit(
                "sbx_capture_soft",
                message=(
                    "SBX capture started — charge_rate verification deferred until "
                    "after commissioning (decommissioned Drive may have no frames yet)"
                ),
            )

    def _wait_sbx_frames(self, *, timeout_sec: float = 25.0) -> bool:
        """Poll Modbus capture until decoded SBX frames appear (or timeout).

        Capture start races the serial open / first frame — checking once
        immediately often reports "no frames yet" even when the bus is healthy.
        """
        import dashboard_logs as dlog

        if self._capture_session_dir is None:
            return False
        deadline = time.monotonic() + float(timeout_sec)
        self._emit(
            "sbx_frames_wait",
            timeout_sec=timeout_sec,
            message=(
                f"Waiting up to {timeout_sec:.0f}s for first SBX Modbus frames "
                "before capture verdict"
            ),
        )
        while time.monotonic() < deadline:
            if self._stop_check():
                break
            try:
                if dlog.sbx_modbus_has_frames(self._capture_session_dir):
                    self._emit(
                        "sbx_frames_ready",
                        message="SBX Modbus frames present in capture log",
                    )
                    return True
            except Exception:
                pass
            time.sleep(1.0)
        return False

    def _emit_capture_stream_status(self, *, wait_sbx_frames_sec: float = 25.0) -> None:
        """Report SBX / CAN / UART start + data availability for the report."""
        import dashboard_logs as dlog
        from datetime import datetime, timezone

        started = datetime.now(timezone.utc).isoformat()
        health: dict[str, Any] = {}
        try:
            health = dlog.unified_status()
        except Exception as exc:
            health = {"error": str(exc)}
        uart: dict[str, Any] = {}
        try:
            uart = dlog.uart_status()
        except Exception as exc:
            uart = {"error": str(exc)}

        # Give SBX time to produce the first decoded frame before the JAMA case.
        has_frames = False
        if self._capture_session_dir is not None:
            try:
                has_frames = bool(dlog.sbx_modbus_has_frames(self._capture_session_dir))
            except Exception:
                has_frames = False
            if not has_frames and wait_sbx_frames_sec > 0:
                has_frames = self._wait_sbx_frames(timeout_sec=wait_sbx_frames_sec)

        streams = [
            {
                "stream": "sbx",
                "started_at": started,
                "ok": health.get("modbus_ok") is not False and bool(
                    health.get("running") or self._capture_session_dir
                ),
                "data_available": has_frames,
                "error": (
                    None
                    if has_frames
                    else (
                        "; ".join(str(w) for w in (health.get("warnings") or []))
                        or health.get("error")
                        or (
                            f"no SBX frames after {wait_sbx_frames_sec:.0f}s wait "
                            "(may appear after commissioning)"
                        )
                    )
                ),
            },
            {
                "stream": "can",
                "started_at": started,
                "ok": health.get("can_ok") is not False,
                "data_available": bool(health.get("can_ok")),
                "error": (
                    None
                    if health.get("can_ok") is not False
                    else (health.get("error") or "CAN not available / masked")
                ),
            },
            {
                "stream": "uart",
                "started_at": started,
                "ok": bool(uart.get("running")),
                "data_available": bool(uart.get("running")),
                "error": None if uart.get("running") else (uart.get("error") or "UART not running"),
            },
        ]
        for row in streams:
            self._emit(
                "capture_stream_status",
                **row,
                message=(
                    f"{row['stream'].upper()} capture started @ {started} — "
                    f"data_available={row['data_available']}"
                    + (f" error={row['error']}" if row.get("error") and not row["data_available"] else "")
                ),
            )
            if row["stream"] == "sbx":
                expected = "SBX Modbus frames present in capture log"
                if row.get("data_available"):
                    observed = "frames present"
                    soft_ok = True
                else:
                    observed = (
                        f"capture running; no frames after "
                        f"{wait_sbx_frames_sec:.0f}s (re-check after commission)"
                    )
                    # Soft: pre-commission empty bus is not a hard fail.
                    soft_ok = bool(row.get("ok"))
            else:
                expected = "capture started"
                soft_ok = bool(row.get("ok"))
                observed = (
                    "started"
                    + (
                        "; data available"
                        if row.get("data_available")
                        else "; no data yet"
                    )
                )
            emit_jama_case(
                self._emit,
                case_id=self._case_ids.next("CAP"),
                section="Capture",
                title=f"Capture stream: {row['stream'].upper()}",
                signal=str(row["stream"]),
                expected=expected,
                observed=observed,
                sources=str(row["stream"]),
                result="pass" if soft_ok else "fail",
                evidence=[],
                show_evidence=False,
            )
        self._emit(
            "capture_status_summary",
            streams=streams,
            session_dir=str(self._capture_session_dir or ""),
            message="Capture stream availability checked",
        )

    def _emit_sbx_capture_recheck(self, *, label: str = "after commission") -> None:
        """Emit a follow-up Capture case once SBX has had time to produce frames."""
        import dashboard_logs as dlog

        has_frames = False
        if self._capture_session_dir is not None:
            try:
                has_frames = bool(dlog.sbx_modbus_has_frames(self._capture_session_dir))
            except Exception:
                has_frames = False
        cr = self._read_charge_rate()
        if has_frames:
            observed = (
                f"frames present; charge_rate {cr} A"
                if cr is not None
                else "frames present"
            )
        else:
            observed = f"still no SBX frames {label}"
        emit_jama_case(
            self._emit,
            case_id=self._case_ids.next("CAP"),
            section="Capture",
            title=f"SBX capture re-check ({label})",
            signal="sbx",
            expected="SBX Modbus frames present in capture log",
            observed=observed,
            sources="sbx",
            result="pass" if has_frames else "fail",
            evidence=[],
            show_evidence=False,
            metrics={"charge_rate_a": cr} if cr is not None else dict(),
        )

    def _run_panel_ratings_phase(self) -> None:
        """Phase 4 — read panel ratings (no writes) and sample live feeder amps."""
        assert self.gateway is not None
        self._set_pipeline(
            "panel_ratings",
            message="Reading site utility / conductor / busbar ratings; sampling feeder amps",
        )
        from drive_automation.panel_ratings import (
            FeederAmpsWatcher,
            read_panel_ratings,
            sample_feeder_amps,
        )

        ratings = read_panel_ratings(self.gateway, emit=self._emit)
        if ratings.get("site_utility_rating_a") is not None:
            self._site_capacity_a = float(ratings["site_utility_rating_a"])
            self.params.site_capacity_a = self._site_capacity_a
        if ratings.get("limit_a") is not None:
            self._panel_limit_a = float(ratings["limit_a"])
            self.params.panel_limit_a = self._panel_limit_a
        self.params.conductor_rating_a = ratings.get("conductor_rating_a")
        self.params.busbar_rating_a = ratings.get("busbar_rating_a")
        self.params.feeder_breaker_rating_a = ratings.get("feeder_breaker_rating_a")
        self.params.throttle_threshold_a = ratings.get("throttle_threshold_a")

        sample = sample_feeder_amps(self.gateway, timeout_s=45.0, emit=self._emit)
        self._emit(
            "panel_limit_meter",
            limit_a=self._panel_limit_a,
            throttle_threshold_a=self.params.throttle_threshold_a,
            feeder_combined_a=(sample or {}).get("combined_a"),
            message=(
                f"Panel meter max={self._panel_limit_a} A "
                f"(80% throttle threshold={self.params.throttle_threshold_a} A)"
            ),
        )
        try:
            self._feeder_watcher = FeederAmpsWatcher(
                self.gateway, self._emit, interval_sec=8.0
            )
            self._feeder_watcher.start()
        except Exception as exc:
            self._emit(
                "feeder_amps_error",
                error=str(exc),
                message=f"Could not start feeder amps watcher: {exc}",
            )

    def _await_sbx_ready(
        self, timeout_sec: float = 45.0, *, hard_fail: bool = True
    ) -> None:
        """Block until Modbus/SBX is healthy and charge_rate appears (or fail).

        start_unified returns before device probe finishes, so without this wait
        the pipeline can run blind on a header-only Modbus log.
        """
        import dashboard_logs as dlog

        deadline = time.monotonic() + float(timeout_sec)
        last_health: dict[str, Any] = dict()
        self._emit(
            "sbx_wait_start",
            timeout_sec=timeout_sec,
            hard_fail=hard_fail,
            message=f"Waiting up to {timeout_sec:.0f}s for SBX charge_rate in Modbus log",
        )
        while time.monotonic() < deadline:
            if self._stop_check():
                raise DashboardError("Cancelled while waiting for SBX capture")
            try:
                health = dlog.unified_status()
            except Exception as exc:
                health = {"error": str(exc)}
            last_health = health
            if health.get("modbus_ok") is False:
                warnings = health.get("warnings") or []
                self._validate(
                    "sbx_modbus_health",
                    ok=False,
                    message=(
                        "SBX / Modbus capture did not start: "
                        + (
                            "; ".join(str(w) for w in warnings)
                            or str(health.get("error") or "modbus_ok=false")
                        )
                    ),
                    expected="modbus_ok=true",
                    actual=health.get("modbus_ok"),
                    detail={"warnings": warnings},
                    hard_fail=hard_fail,
                )
                if not hard_fail:
                    return
            cr = self._read_charge_rate()
            has_frames = False
            if self._capture_session_dir is not None:
                try:
                    has_frames = bool(
                        dlog.sbx_modbus_has_frames(self._capture_session_dir)
                    )
                except Exception:
                    has_frames = False
            if cr is not None:
                self._emit(
                    "sbx_ready",
                    charge_rate_a=cr,
                    message=f"SBX ready — charge_rate {cr} A",
                )
                self._validate(
                    "sbx_charge_rate_live",
                    ok=True,
                    message=f"SBX charge_rate live ({cr} A)",
                    expected="charge_rate_a present in Modbus/SBX log",
                    actual=cr,
                )
                return
            time.sleep(1.0)

        has_frames = False
        if self._capture_session_dir is not None:
            try:
                has_frames = bool(dlog.sbx_modbus_has_frames(self._capture_session_dir))
            except Exception:
                has_frames = False
        detail = (
            "Modbus log has frames but no charge_rate_a yet"
            if has_frames
            else "Modbus log is header-only / undecoded (check baud 57600, port reclaim)"
        )
        self._validate(
            "sbx_charge_rate_live",
            ok=False,
            message=(
                f"No SBX charge_rate within {timeout_sec:.0f}s after capture start "
                f"({detail})."
            ),
            expected="charge_rate_a within timeout",
            actual={
                "modbus_ok": last_health.get("modbus_ok"),
                "has_frames": has_frames,
                "warnings": last_health.get("warnings"),
            },
            hard_fail=hard_fail,
        )

    def _read_charge_rate_detail(self) -> Optional[dict[str, Any]]:
        """Latest charge_rate with Modbus evidence line (for JAMA cases)."""
        if self._capture_session_dir is not None:
            try:
                import dashboard_logs as dlog

                detail = dlog.latest_sbx_charge_rate_detail(self._capture_session_dir)
                if detail is not None:
                    return detail
            except Exception:
                pass
        # MQTT fallback — no raw Modbus line available.
        cr = None
        if self.panel is not None:
            try:
                snap = self.panel.poll_status()
                if snap.charge_rate_a is not None:
                    cr = float(snap.charge_rate_a)
            except Exception:
                cr = None
        if cr is None:
            return None
        return {
            "amps": int(cr) if float(cr).is_integer() else cr,
            "evidence": f"[mqtt fallback] charge_rate_a={cr}",
            "source": "mqtt",
            "modbus_log": None,
            "ts": "",
        }

    def _read_charge_rate(self) -> Optional[float]:
        """Latest panel->drive commanded current (A).

        Prefer the live Modbus/SBX log (USB sniff or gateway mirror). Fall back
        to gateway eve MQTT — wall FTDI is often silent with no RS-485 tap.
        """
        detail = self._read_charge_rate_detail()
        if detail is not None and detail.get("amps") is not None:
            try:
                return float(detail["amps"])
            except (TypeError, ValueError):
                pass
        return None

    def _read_panel_capacity(self) -> Optional[float]:
        """Site utility rating (A) via span-pcm mt panel get — panel capacity limit.

        Optional: when gateway is disabled or unreachable, continue without a
        site-capacity value so EV/LB2 throttle runs still complete.
        """
        if not self.config.use_gateway or self.gateway is None:
            return None
        from drive_automation.commissioning import get_panel_capacity

        try:
            info = get_panel_capacity(self.gateway, emit=self._emit)
        except GatewayError as exc:
            self._emit(
                "panel_capacity_error",
                message=(
                    "Gateway unreachable — continuing without site utility rating. "
                    f"{exc}"
                ),
            )
            return None
        except Exception as exc:
            self._emit(
                "panel_capacity_error",
                message=f"Panel capacity read failed — continuing without it: {exc}",
            )
            return None
        cap = info.get("site_utility_rating_a")
        if cap is not None:
            self._site_capacity_a = float(cap)
            self.params.site_capacity_a = float(cap)
        return self._site_capacity_a

    @staticmethod
    def _pilot_fields(reading: dict[str, Any]) -> dict[str, Any]:
        return {
            "frequency_hz": reading.get("frequency_hz"),
            "duty_cycle_pct": reading.get("duty_cycle_pct"),
            "cp_high_v": reading.get("cp_voltage_v"),
            "cp_low_v": reading.get("cp_low_v"),
            "adv_current_a": reading.get("max_current_a"),
        }


def _as_opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
