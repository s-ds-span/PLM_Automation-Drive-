"""Local test dashboard for drive automation (SSE + run control)."""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from flask import Flask, Response, jsonify, render_template, request, send_file

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from drive_automation import __version__
from drive_automation.artifacts import resolve_artifact_dir
from drive_automation.commissioning import decommission_drive
from drive_automation.config import DriveConfig
from drive_automation.gateway import build_gateway_shell_from_env
from drive_automation.load_selection import normalize_channel_voltages
from drive_automation.case_selection import ALL_JAMA_CASES, CASE_LABELS, parse_cases_csv
from drive_automation.jama_pipeline import JAMA_PIPELINE_PHASES
from drive_automation.orchestrator import (
    PIPELINE_PHASES,
    TestParams,
    ThrottleTestOrchestrator,
    check_availability,
)
from drive_automation.safety import safe_shutdown_units

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
)

_run_lock = threading.Lock()
_run_thread: threading.Thread | None = None
_decom_thread: threading.Thread | None = None
_active_orch: ThrottleTestOrchestrator | None = None
_cancel_flag = threading.Event()
_event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2000)
_run_state: dict[str, Any] = {"status": "idle", "message": "", "pipeline_phase": "idle"}

# Soft controller lock so additional dashboard tabs enter viewer-only mode.
_CONTROLLER_TTL_SEC = 45.0
_controller_lock = threading.Lock()
_controller: Optional[dict[str, Any]] = None  # client_id, last_seen, label
_run_owner_id: Optional[str] = None


def _broadcast(event: dict[str, Any]) -> None:
    try:
        _event_queue.put_nowait(event)
    except queue.Full:
        try:
            _event_queue.get_nowait()
        except queue.Empty:
            pass
        _event_queue.put_nowait(event)


def _purge_stale_controller(now: Optional[float] = None) -> None:
    global _controller
    ts = now if now is not None else time.time()
    if _controller and (ts - float(_controller.get("last_seen") or 0)) > _CONTROLLER_TTL_SEC:
        _controller = None


def _is_controller(client_id: str) -> bool:
    with _controller_lock:
        _purge_stale_controller()
        return bool(_controller and _controller.get("client_id") == client_id)


def _role_payload(client_id: str) -> dict[str, Any]:
    with _controller_lock:
        _purge_stale_controller()
        running = bool(_run_thread and _run_thread.is_alive())
        # Thread may have died while status was left at "running" (kill/restart).
        if not running and _run_state.get("status") in ("running", "stopping"):
            _run_state["status"] = "idle"
            if not _run_state.get("message"):
                _run_state["message"] = "No active run"
        is_ctrl = bool(_controller and _controller.get("client_id") == client_id)
        role = "controller" if is_ctrl else "viewer"
        reason = ""
        if role == "viewer":
            if running:
                reason = "A test is already running — viewer only"
            elif _controller:
                reason = "Another dashboard client holds control — viewer only"
            else:
                reason = "Claim control to start tests"
        return {
            "ok": True,
            "role": role,
            "client_id": client_id,
            "controller_id": (_controller or {}).get("client_id"),
            "controller_label": (_controller or {}).get("label") or "",
            "run_owner_id": _run_owner_id,
            "running": running,
            "run_status": dict(_run_state),
            "viewer_reason": reason,
            "version": __version__,
            "pipeline_phases": list(PIPELINE_PHASES),
            "jama_pipeline_phases": list(JAMA_PIPELINE_PHASES),
            "jama_cases": [
                {"id": cid, "label": CASE_LABELS.get(cid, cid)}
                for cid in ALL_JAMA_CASES
            ],
        }


def _run_worker(params: TestParams, run_id: str) -> None:
    global _run_state, _active_orch, _run_owner_id
    config = DriveConfig.from_env()
    config.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = config.log_dir / f"{run_id}.jsonl"
    from drive_automation.lab_capture import LabStreamCapture

    lab_cap = LabStreamCapture(
        log_path.parent,
        session_id=run_id,
        tz_name=config.drive_timezone,
    )
    lab_cap.attach_automation()
    pilot_log_path = lab_cap.paths["pilot"]
    _run_state = {
        "status": "running",
        "message": "Test running",
        "pipeline_phase": "idle",
        "run_id": run_id,
        "failed_pipeline_phase": None,
        "phase": None,
        "iteration": None,
    }
    emit_lock = threading.Lock()

    def emit(event: str, **payload: Any) -> None:
        from drive_automation.telemetry import append_pilot_capture_line
        from drive_automation.timeutil import stamp_fields

        stamps = stamp_fields(config.drive_timezone)
        record = {
            **stamps,
            "event": event,
            "run_id": run_id,
            **payload,
        }
        # Preserve stamps / Modbus-line timestamps when the caller already set them.
        if payload.get("ts") and event in ("sbx_abnormality",):
            record["log_ts"] = payload.get("ts")
            record["ts"] = stamps["ts"]
            record["ts_local"] = stamps["ts_local"]
        elif payload.get("ts"):
            record["ts"] = payload["ts"]
            if payload.get("ts_local"):
                record["ts_local"] = payload["ts_local"]
            if payload.get("tz"):
                record["tz"] = payload["tz"]
        line = json.dumps(record, default=str)
        print(line, flush=True)
        with emit_lock:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
            append_pilot_capture_line(pilot_log_path, record)
            lab_cap.ingest_event(record)
        _broadcast(record)
        if event != "telemetry":
            _run_state["message"] = str(payload.get("message") or event)
            if event == "phase":
                _run_state["phase"] = payload.get("phase")
                _run_state["phase_duration_sec"] = payload.get("duration_sec")
                _run_state["phase_started_at"] = payload.get("started_at")
                _run_state["iteration"] = payload.get("iteration")
            if event == "pipeline_phase":
                _run_state["pipeline_phase"] = payload.get("phase")
            if event == "load_setpoint":
                _run_state["setpoint_amps"] = payload.get("setpoint_amps")
                _run_state["setpoint_source"] = payload.get("source")
            if event in ("aggregation_sample", "throttle_engaged", "throttle_step_6a",
                         "throttle_zero", "recovery_step", "panel_status"):
                if payload.get("charge_rate_a") is not None:
                    _run_state["charge_rate_a"] = payload.get("charge_rate_a")
            if event == "ceiling_override_prompt":
                _run_state["ceiling_override_pending"] = {
                    k: payload.get(k)
                    for k in (
                        "iteration",
                        "commanded_amps",
                        "achieved_amps",
                        "meter_amps",
                        "max_lb2_amps",
                        "hardware_max_amps",
                        "at_hardware_max",
                        "charge_rate_a",
                        "duty_cycle_pct",
                        "suggested_ceiling_a",
                        "message",
                    )
                }
            if event in (
                "ceiling_override_accepted",
                "ceiling_override_declined",
                "ceiling_override_timeout",
                "ceiling_override_cancelled",
                "ceiling_override_cleared",
                "aggregation_hardware_saturated",
            ):
                _run_state.pop("ceiling_override_pending", None)

    orch = ThrottleTestOrchestrator(
        config,
        params,
        emit=emit,
        log_path=log_path,
        stop_check=_cancel_flag.is_set,
    )
    orch._lab_capture = lab_cap
    orch._pilot_log_path = lab_cap.paths["pilot"]
    _active_orch = orch
    try:
        orch.run()
        _run_state = {
            **dict(_run_state),
            "status": "complete",
            "message": "Test complete",
            "run_id": run_id,
        }
        _broadcast({
            "event": "dashboard_status",
            "status": "complete",
            "run_id": run_id,
        })
    except Exception as exc:
        # Orchestrator already emitted structured run_failed before packaging.
        pipe = _run_state.get("pipeline_phase") or getattr(orch, "pipeline_phase", None)
        phase = _run_state.get("phase") or getattr(orch, "_phase", None)
        iteration = _run_state.get("iteration") or getattr(orch, "_iteration", None)
        _run_state = {
            **dict(_run_state),
            "status": "error",
            "message": str(exc),
            "run_id": run_id,
            "failed_pipeline_phase": pipe,
            "phase": phase,
            "iteration": iteration,
        }
        _broadcast({
            "event": "dashboard_status",
            "status": "error",
            "run_id": run_id,
            "message": str(exc),
        })
    finally:
        _cancel_flag.clear()
        _run_owner_id = None
        _active_orch = None
        _run_state.pop("ceiling_override_pending", None)
        _broadcast({
            "event": "ceiling_override_cleared",
            "run_id": run_id,
            "message": "Ceiling override prompt cleared (run ended)",
        })


@app.route("/")


def index():
    return render_template(
        "index.html",
        app_version=__version__,
        copyright_year=2026,
    )


@app.route("/api/version")


def api_version():
    return jsonify({"ok": True, "version": __version__})


@app.route("/api/role", methods=["POST"])


def api_role():
    """Claim / heartbeat / release dashboard controller role."""
    global _controller
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id") or "").strip() or str(uuid.uuid4())
    action = str(data.get("action") or "claim").strip().lower()
    label = str(data.get("label") or "").strip()[:40]
    now = time.time()

    with _controller_lock:
        _purge_stale_controller(now)
        if action == "release":
            if _controller and _controller.get("client_id") == client_id:
                _controller = None
                _broadcast(
                    {
                        "event": "controller_released",
                        "message": "Dashboard controller released",
                    }
                )
        elif action in ("claim", "heartbeat"):
            if _controller is None or _controller.get("client_id") == client_id:
                _controller = {
                    "client_id": client_id,
                    "last_seen": now,
                    "label": label or (_controller or {}).get("label") or "Operator",
                }

    payload = _role_payload(client_id)
    running = bool(payload.get("running"))
    orch = _active_orch
    if (
        running
        and orch is not None
        and getattr(orch, "_ceiling_override_pending", False)
        and getattr(orch, "_ceiling_override_prompt", None)
    ):
        payload["ceiling_override_pending"] = dict(orch._ceiling_override_prompt)
    elif running and _run_state.get("ceiling_override_pending"):
        payload["ceiling_override_pending"] = dict(_run_state["ceiling_override_pending"])
    else:
        # Never resurface a stale prompt after the run ends.
        payload["ceiling_override_pending"] = None
        if not running:
            _run_state.pop("ceiling_override_pending", None)
    if action == "claim" and payload["role"] == "controller":
        _broadcast(
            {
                "event": "controller_claimed",
                "client_id": client_id,
                "label": label,
                "message": "Dashboard controller claimed",
            }
        )
    return jsonify(payload)


@app.route("/api/availability")


def api_availability():
    config = DriveConfig.from_env()
    try:
        avail = check_availability(config)
        return jsonify({"ok": True, **avail.to_dict()})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502


@app.route("/api/status")


def api_status():
    client_id = str(request.args.get("client_id") or "").strip()
    payload: dict[str, Any] = {"ok": True, **_run_state, "version": __version__}
    if client_id:
        payload.update(_role_payload(client_id))
    else:
        running = bool(_run_thread and _run_thread.is_alive())
        payload["running"] = running
    return jsonify(payload)


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_lb2_voltages(data: dict[str, Any]) -> dict[int, str]:
    """Read per-channel 120/240 V selections from the dashboard form/API body."""
    raw: dict[int, str] = {}
    nested = data.get("lb2_channel_voltages")
    if isinstance(nested, dict):
        for key, value in nested.items():
            try:
                ch = int(key)
            except (TypeError, ValueError):
                continue
            raw[ch] = str(value)
    for ch in (1, 2, 3, 4):
        for key in (f"lb2_v_ch{ch}", f"lb2_ch{ch}_v", f"ch{ch}_v"):
            if key in data and data[key] not in (None, ""):
                raw[ch] = str(data[key])
                break
    return normalize_channel_voltages(raw)


def _parse_lb2_channels(data: dict[str, Any]) -> tuple[int, ...]:
    """Enabled LB2 channels from checkboxes (``lb2_ch1``…``lb2_ch4``) or list."""
    selected: list[int] = []
    nested = data.get("lb2_channels") or data.get("panel_channels")
    if isinstance(nested, (list, tuple)):
        for item in nested:
            try:
                ch = int(item)
            except (TypeError, ValueError):
                continue
            if ch in (1, 2, 3, 4) and ch not in selected:
                selected.append(ch)
    if not selected:
        for ch in (1, 2, 3, 4):
            raw = data.get(f"lb2_ch{ch}")
            if raw in (None, "", False, "false", "0", "off", "no"):
                continue
            if raw in (True, "true", "1", "on", "yes") or str(raw) == str(ch):
                selected.append(ch)
    return tuple(selected)


@app.route("/api/run", methods=["POST"])


def api_run():
    global _run_thread, _run_owner_id, _run_state
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id") or "").strip()
    if not client_id or not _is_controller(client_id):
        return jsonify(
            {
                "ok": False,
                "error": "Viewer only — another dashboard client holds control.",
                "viewer": True,
            }
        ), 403

    pipeline_mode = str(data.get("pipeline_mode") or "throttle").strip().lower()
    if pipeline_mode not in ("throttle", "jama"):
        pipeline_mode = "throttle"

    use_lb2 = data.get("use_lb2", True)
    if isinstance(use_lb2, str):
        use_lb2 = use_lb2.lower() not in ("false", "0", "no")
    if pipeline_mode == "jama":
        use_lb2 = False

    config = DriveConfig.from_env()
    if not config.ev_password:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "DRIVE_EV_PASSWORD or DRIVE_PASSWORD not set "
                    "(loadbank1 Pi SSH password)."
                ),
            }
        ), 500

    try:
        avail = check_availability(config)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    if not avail.ev_available:
        operator = avail.ev.get("operator") or "another user"
        return jsonify(
            {"ok": False, "error": f"EV Simulator is in use by {operator}."}
        ), 409

    if use_lb2 and not avail.lb2_available:
        # LB2 unavailable is a hard failure (no skip popup).
        return jsonify({"ok": False, "error": "LB2 not available"}), 409

    run_commission = bool(data.get("run_commission")) and pipeline_mode != "jama"
    top_position = _optional_int(data.get("top_position"))
    bottom_position = _optional_int(data.get("bottom_position"))
    breaker_rating_raw = data.get("breaker_rating_a")
    try:
        breaker_rating_a = float(breaker_rating_raw)
    except (TypeError, ValueError):
        breaker_rating_a = 0.0
    if pipeline_mode != "jama" and breaker_rating_a <= 0:
        return jsonify(
            {"ok": False, "error": "Breaker rating (A) is required."}
        ), 400
    if breaker_rating_a <= 0:
        breaker_rating_a = float(config.breaker_rating_a or 0) or 60.0
    if run_commission and (top_position is None or bottom_position is None):
        return jsonify(
            {
                "ok": False,
                "error": "Breaker top and bottom positions are required for commissioning.",
            }
        ), 400
    drive_serial = str(data.get("drive_serial") or config.drive_serial or "").strip()
    if run_commission and not drive_serial:
        return jsonify(
            {
                "ok": False,
                "error": "Drive serial is required when commissioning is enabled.",
            }
        ), 400

    breaker_instance_id = _optional_int(data.get("breaker_instance_id"))
    if breaker_instance_id is None:
        breaker_instance_id = config.breaker_instance_id
    if pipeline_mode == "jama" and breaker_instance_id is None:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "JAMA test requires DER breaker instance id — set "
                    "DRIVE_BREAKER_INSTANCE_ID or pass breaker_instance_id."
                ),
            }
        ), 400

    jama_cases_tuple: tuple[str, ...] | None = None
    if pipeline_mode == "jama":
        raw_cases = data.get("jama_cases") or data.get("drive_cases") or ""
        if isinstance(raw_cases, (list, tuple)):
            parsed = parse_cases_csv(",".join(str(x) for x in raw_cases))
        else:
            parsed = parse_cases_csv(str(raw_cases))
        if parsed is not None:
            jama_cases_tuple = tuple(sorted(parsed))

    # Gateway SSH is mandatory for both pipelines.
    from drive_automation.gateway import build_gateway_shell_from_env

    gw = build_gateway_shell_from_env()
    if not gw.available:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Gateway SSH is required. Prefer spanssh-config "
                    "(~/.ssh/span/<GATEWAY_SERIAL>/spanssh-config) with GATEWAY_SERIAL, "
                    "or set GATEWAY_SSH_HOST + auth in config.env before starting."
                ),
            }
        ), 400
    probe = gw.test_connection()
    if not probe.get("ok"):
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Gateway SSH precheck failed — fix connectivity before starting. "
                    f"{probe.get('message') or probe}"
                ),
            }
        ), 502

    use_dynamic = data.get("use_dynamic_load")
    if use_dynamic is None:
        use_dynamic = True
    panel_channels = _parse_lb2_channels(data)
    if use_lb2 and not panel_channels:
        return jsonify(
            {
                "ok": False,
                "error": "Select at least one LB2 load channel (CH1–CH4).",
            }
        ), 400
    if not panel_channels:
        panel_channels = (1, 2, 3, 4)
    params = TestParams(
        charger_label=str(data.get("charger_label") or "TestCharger"),
        test_name=str(
            data.get("test_name")
            or ("JamaTest01" if pipeline_mode == "jama" else "ThrottleRun01")
        ),
        notes=str(data.get("notes") or ""),
        ev_max_amps=int(data.get("ev_max_amps") or 30),
        lb2_max_amps=float(data.get("lb2_max_amps") or 40),
        # Timeouts are static (not UI-configurable).
        throttle_duration_sec=float(config.sustain_timeout_sec),
        recovery_duration_sec=float(config.recovery_timeout_sec),
        iterations=int(data.get("iterations") or 1),
        lb2_ramp_step_interval_sec=float(data.get("lb2_ramp_interval") or 5),
        lb2_step_a=float(data.get("lb2_step_a") or 5),
        lb2_start_delay_sec=float(data.get("lb2_start_delay") or 10),
        use_lb2=bool(use_lb2),
        panel_channels=panel_channels,
        site_utility_rating_ma=int(
            data.get("site_utility_rating_ma") or config.site_utility_rating_ma
        ),
        conductor_rating_ma=int(
            data.get("conductor_rating_ma") or config.conductor_rating_ma
        ),
        feeder_breaker_rating_a=float(
            data.get("feeder_breaker_rating_a") or config.feeder_breaker_rating_a
        ),
        lb2_channel_voltages=_parse_lb2_voltages(data),
        run_commission=run_commission,
        drive_serial=drive_serial,
        breaker_rating_a=breaker_rating_a,
        breaker_instance_id=breaker_instance_id,
        pipeline_mode=pipeline_mode,
        jama_cases=jama_cases_tuple,
        top_position=top_position,
        bottom_position=bottom_position,
        phase_delay_sec=float(data.get("phase_delay") or config.phase_delay_sec),
        ev_draw_fraction=float(data.get("ev_draw_fraction") or config.ev_draw_fraction),
        drive_timezone=str(data.get("drive_timezone") or config.drive_timezone),
        load_loop_interval_sec=float(
            data.get("load_loop_interval") or config.load_loop_interval_sec
        ),
        aggregation_timeout_sec=float(config.aggregation_timeout_sec),
        sustain_timeout_sec=float(config.sustain_timeout_sec),
        use_dynamic_load=bool(use_dynamic),
    )

    from drive_automation.timeutil import now_local

    run_id = f"run_{now_local(config.drive_timezone).strftime('%Y%m%d_%H%M%S')}"
    with _run_lock:
        if _run_thread and _run_thread.is_alive():
            return jsonify({"ok": False, "error": "A test is already running."}), 409
        _cancel_flag.clear()
        _run_owner_id = client_id
        _run_state = {
            "status": "running",
            "message": "Test started",
            "pipeline_phase": "idle",
            "run_id": run_id,
        }
        _run_thread = threading.Thread(
            target=_run_worker,
            args=(params, run_id),
            daemon=True,
        )
        _run_thread.start()

    return jsonify({
        "ok": True,
        "message": (
            "JAMA test started"
            if pipeline_mode == "jama"
            else "Test started"
        ),
        "use_lb2": params.use_lb2,
        "pipeline_mode": params.pipeline_mode,
        "run_id": run_id,
    })


@app.route("/api/gateway/test", methods=["POST"])


def api_gateway_test():
    """Probe direct SSH to the SPAN gateway (lab LAN)."""
    try:
        shell = build_gateway_shell_from_env()
        return jsonify(shell.test_connection())
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "message": f"{type(exc).__name__}: {exc}",
            }
        )


@app.route("/api/decommission", methods=["POST"])


def api_decommission():
    """
    Separate maintenance action — NOT part of the one-click pipeline.
    Requires controller role and confirmation flag.
    """
    global _decom_thread
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id") or "").strip()
    if not client_id or not _is_controller(client_id):
        return jsonify(
            {
                "ok": False,
                "error": "Viewer only — cannot decommission from this client.",
                "viewer": True,
            }
        ), 403
    if not data.get("confirm"):
        return jsonify(
            {"ok": False, "error": "Pass confirm: true to run decommission."}
        ), 400
    if _run_thread and _run_thread.is_alive():
        return jsonify(
            {"ok": False, "error": "Cannot decommission while one-click pipeline is running."}
        ), 409
    if _decom_thread and _decom_thread.is_alive():
        return jsonify({"ok": False, "error": "Decommission already running."}), 409

    config = DriveConfig.from_env()
    if not config.use_gateway:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Decommission needs gateway SSH. Ensure USE_GATEWAY=1 and "
                    "GATEWAY_SERIAL + spanssh-config (or legacy GATEWAY_SSH_HOST) "
                    "in config.env."
                ),
            }
        ), 400

    shell = build_gateway_shell_from_env()
    if not shell.available:
        return jsonify(
            {
                "ok": False,
                "error": (
                    "Gateway SSH not available — set GATEWAY_SERIAL with "
                    "~/.ssh/span/<serial>/spanssh-config on the lab cell."
                ),
            }
        ), 500

    breaker_id = _optional_int(data.get("breaker_instance_id"))
    top_bc = _optional_int(data.get("top_bc_instance"))
    bottom_bc = _optional_int(data.get("bottom_bc_instance"))

    def _worker() -> None:
        def emit(event: str, **payload: Any) -> None:
            from datetime import datetime, timezone

            record = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **payload,
            }
            _broadcast(record)

        try:
            result = decommission_drive(
                shell,
                breaker_instance_id=breaker_id,
                top_bc_instance=top_bc,
                bottom_bc_instance=bottom_bc,
                emit=emit,
            )
            _broadcast({"event": "decommission_finished", **result})
        except Exception as exc:
            _broadcast(
                {
                    "event": "decommission_finished",
                    "ok": False,
                    "error": str(exc),
                    "message": f"Decommission failed: {exc}",
                }
            )

    _decom_thread = threading.Thread(target=_worker, daemon=True)
    _decom_thread.start()
    return jsonify(
        {
            "ok": True,
            "accepted": True,
            "message": "Decommission started (separate from one-click pipeline)",
        }
    )


@app.route("/api/run/ceiling-override", methods=["POST"])


def api_ceiling_override():
    """Operator answer to Dynamic LB2 ceiling-raise prompt during aggregation."""
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id") or "").strip()
    if not client_id or not _is_controller(client_id):
        return jsonify(
            {
                "ok": False,
                "error": "Viewer only — cannot answer ceiling override from this client.",
                "viewer": True,
            }
        ), 403
    orch = _active_orch
    if orch is None or not (_run_thread and _run_thread.is_alive()):
        return jsonify({"ok": False, "error": "No active pipeline run."}), 409
    allow = bool(data.get("allow"))
    new_ceiling = data.get("new_ceiling_a")
    result = orch.resolve_ceiling_override(allow=allow, new_ceiling_a=new_ceiling)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.route("/api/stop", methods=["POST"])


def api_stop():
    """Stop the running automation loop (orchestrator finally still safe-exits)."""
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id") or "").strip()
    if client_id and not _is_controller(client_id):
        return jsonify(
            {
                "ok": False,
                "error": "Viewer only — cannot stop from this client.",
                "viewer": True,
            }
        ), 403

    running = bool(_run_thread and _run_thread.is_alive())
    _cancel_flag.set()
    _run_state["status"] = "stopping" if running else _run_state.get("status", "idle")
    _run_state["message"] = "Stop requested — finishing current step then safe exit"
    _broadcast(
        {
            "event": "stop_requested",
            "running": running,
            "message": "Stop automation requested",
        }
    )
    return jsonify(
        {
            "ok": True,
            "running": running,
            "message": (
                "Stop requested — test will safe-exit after the current step"
                if running
                else "No test was running"
            ),
        }
    )


@app.route("/api/safe-shutdown", methods=["POST"])


def api_safe_shutdown():
    """Safe-exit EV and/or LB2 independently of a test run (safety buttons)."""
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id") or "").strip()
    if client_id and not _is_controller(client_id):
        return jsonify(
            {
                "ok": False,
                "error": "Viewer only — cannot safe-exit from this client.",
                "viewer": True,
            }
        ), 403

    unit = str(data.get("unit") or "both").strip().lower()
    if unit not in ("ev", "lb2", "both"):
        return jsonify({"ok": False, "error": "unit must be ev, lb2, or both"}), 400

    _broadcast(
        {
            "event": "safe_shutdown_started",
            "unit": unit,
            "message": f"Manual safe shutdown started ({unit})",
        }
    )

    def _worker() -> None:
        global _active_orch, _run_state
        try:
            if _run_thread and _run_thread.is_alive():
                _cancel_flag.set()
                _broadcast(
                    {
                        "event": "stop_requested",
                        "message": "Stop requested — waiting for orchestrator safe exit",
                    }
                )
                _run_thread.join(timeout=180)
                if _run_thread.is_alive():
                    _broadcast(
                        {
                            "event": "safe_shutdown_complete",
                            "ok": False,
                            "unit": unit,
                            "message": (
                                "Orchestrator still running after stop — "
                                "use ./release_units.sh --emergency"
                            ),
                        }
                    )
                    _run_state["message"] = (
                        "Safe shutdown timed out waiting for orchestrator"
                    )
                    return
                _active_orch = None
                _broadcast(
                    {
                        "event": "safe_shutdown_complete",
                        "ok": True,
                        "unit": unit,
                        "message": "Orchestrator stopped and completed its safe exit",
                        "via": "orchestrator_finally",
                    }
                )
                _run_state["message"] = "Safe shutdown complete"
                return

            orch = _active_orch
            result = safe_shutdown_units(
                unit,
                ev_client=orch.ev if orch else None,
                lb2_client=orch.lb2 if orch else None,
            )
            _broadcast({"event": "safe_shutdown_complete", **result})
            if result.get("ok"):
                _run_state["message"] = result.get("message") or "Safe shutdown complete"
                _active_orch = None
            else:
                _run_state["message"] = result.get("message") or "Safe shutdown failed"
        except Exception as exc:
            _broadcast(
                {
                    "event": "safe_shutdown_complete",
                    "ok": False,
                    "unit": unit,
                    "error": str(exc),
                    "message": f"Safe shutdown error: {exc}",
                }
            )

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify(
        {
            "ok": True,
            "accepted": True,
            "unit": unit,
            "message": f"Safe shutdown started for {unit} (see event log)",
        }
    )


def _resolve_capture_body(body: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve wall SSH creds from gateway_serial + panels_config.yml."""
    import capture_gateway_config as capgw

    return capgw.resolve_capture_credentials(body or {})


@app.route("/api/capture/gateways")


def api_capture_gateways():
    try:
        import capture_gateway_config as capgw

        return jsonify(capgw.list_capture_gateways())
    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "gateways": [],
                "load_error": f"{type(exc).__name__}: {exc}",
                "password_configured": False,
                "config_exists": False,
            }
        )


@app.route("/api/capture/targets")


def api_capture_targets():
    """Legacy alias — prefer /api/capture/gateways."""
    import capture_gateway_config as capgw

    data = capgw.list_capture_gateways()
    return jsonify(data)


@app.route("/api/logs/unified/start", methods=["POST"])
def api_unified_start():
    import os

    import dashboard_logs as dlog

    body = request.get_json(silent=True) or {}
    creds = _resolve_capture_body(body)
    if not creds.get("ok"):
        return jsonify(creds), 400
    # Default matches automation: skip CAN when PCAN is not on the wall
    # (DRIVE_MASK_CANBUS=1). UI can force CAN with skip_can=false.
    if "skip_can" in body:
        skip_can = bool(body.get("skip_can"))
    else:
        skip_can = (os.environ.get("DRIVE_MASK_CANBUS") or "1").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    result = dlog.start_unified(
        username=creds["username"],
        host=creds["host"],
        password=creds["password"],
        skip_can=skip_can,
    )
    if result.get("ok"):
        result["skip_can"] = skip_can
    code = 200 if result.get("ok") else 400
    return jsonify(result), code


@app.route("/api/logs/unified/stop", methods=["POST"])


def api_unified_stop():
    import dashboard_logs as dlog

    result = dlog.stop_unified()
    code = 200 if result.get("ok") else 400
    return jsonify(result), code


@app.route("/api/logs/unified/status")


def api_unified_status():
    import dashboard_logs as dlog

    return jsonify(dlog.unified_status())


@app.route("/api/logs/unified/panel_current")


def api_unified_panel_current():
    import dashboard_logs as dlog

    return jsonify(dlog.panel_current_now())


@app.route("/api/logs/unified/tail")


def api_unified_tail():
    import dashboard_logs as dlog

    kind = request.args.get("kind", "can")
    since_byte = int(request.args.get("since_byte") or 0)
    lines_raw = request.args.get("lines")
    max_lines = int(lines_raw) if lines_raw not in (None, "") else None
    return jsonify(
        dlog.unified_capture_tail(
            kind=kind,
            since_byte=since_byte,
            max_lines=max_lines,
        )
    )


@app.route("/api/logs/unified/download")


def api_unified_download():
    import dashboard_logs as dlog

    sid = request.args.get("session_id") or ""
    rel = request.args.get("rel") or ""
    try:
        path = dlog.safe_session_download(sid, rel)
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/api/logs/uart/start", methods=["POST"])


def api_uart_start():
    import dashboard_logs as dlog

    body = request.get_json(silent=True) or {}
    creds = _resolve_capture_body(body)
    if not creds.get("ok"):
        return jsonify(creds), 400
    strings = body.get("strings") or []
    if isinstance(strings, str):
        strings = [s.strip() for s in strings.splitlines() if s.strip()]
    result = dlog.start_uart_capture(
        username=creds["username"],
        host=creds["host"],
        password=creds["password"],
        strings=list(strings),
    )
    code = 200 if result.get("ok") else 400
    return jsonify(result), code


@app.route("/api/logs/uart/stop", methods=["POST"])


def api_uart_stop():
    import dashboard_logs as dlog

    result = dlog.stop_uart_capture()
    code = 200 if result.get("ok") else 400
    return jsonify(result), code


@app.route("/api/logs/uart/status")


def api_uart_status():
    import dashboard_logs as dlog

    return jsonify(dlog.uart_status())


@app.route("/api/logs/uart/download")


def api_uart_download():
    import dashboard_logs as dlog

    path = request.args.get("path") or ""
    try:
        file_path = dlog.uart_resolve_existing_file(path)
    except FileNotFoundError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return send_file(file_path, as_attachment=True, download_name=file_path.name)


@app.route("/api/events")


def api_events():
    def stream():
        # Flush headers immediately so EventSource leaves CONNECTING.
        yield ": connected\n\n"
        while True:
            try:
                item = _event_queue.get(timeout=30)
                yield f"data: {json.dumps(item, default=str)}\n\n"
            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/lab-capture/start", methods=["POST"])
def api_lab_capture_start():
    """Start manual multi-stream capture (pilot / EV loads / LB2 / panel)."""
    from drive_automation.lab_capture import start_manual_lab_capture

    config = DriveConfig.from_env()
    body = request.get_json(silent=True) or {}
    streams = body.get("streams")
    if isinstance(streams, str):
        streams = [s.strip() for s in streams.split(",") if s.strip()]
    result = start_manual_lab_capture(
        log_root=config.log_dir,
        tz_name=config.drive_timezone,
        streams=list(streams) if streams else None,
        interval_sec=float(body.get("interval_sec") or 1.0),
        login_ev=bool(body.get("login_ev", True)),
        login_lb2=bool(body.get("login_lb2", True)),
        use_gateway=bool(body.get("use_gateway", True)),
        # Default false: stream checkboxes must not claim/start EV or LB2 loads.
        take_control=bool(body.get("take_control", False)),
    )
    code = 200 if result.get("running") or result.get("ok") is not False else 400
    return jsonify(result), code


@app.route("/api/lab-capture/stop", methods=["POST"])
def api_lab_capture_stop():
    from drive_automation.lab_capture import stop_manual_lab_capture

    body = request.get_json(silent=True) or {}
    result = stop_manual_lab_capture(logout=bool(body.get("logout", True)))
    return jsonify(result)


@app.route("/api/lab-capture/status")
def api_lab_capture_status():
    from drive_automation.lab_capture import manual_capture_status

    return jsonify(manual_capture_status())


@app.route("/api/lab-capture/download")
def api_lab_capture_download():
    from drive_automation.lab_capture import get_manual_capture

    stream = (request.args.get("stream") or "").strip().lower()
    cap = get_manual_capture()
    if cap is None:
        return jsonify({"ok": False, "error": "No manual capture session."}), 404
    if stream == "zip":
        try:
            path = cap.build_zip()
        except FileNotFoundError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        return send_file(path, as_attachment=True, download_name=path.name)
    path = cap.paths.get(stream)
    if path is None or not path.is_file():
        return jsonify({"ok": False, "error": f"Stream not found: {stream}"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/api/artifacts/<run_id>/download")
def api_artifact_download(run_id: str):
    """Download combined zip (EV test + run JSONL) for a completed lab run."""
    config = DriveConfig.from_env()
    try:
        run_dir = resolve_artifact_dir(config.artifacts_dir, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    combined = run_dir / f"drive_run_{run_id}.zip"
    if not combined.is_file():
        return jsonify({"ok": False, "error": f"Artifact not found for {run_id}"}), 404

    return send_file(
        combined,
        mimetype="application/zip",
        as_attachment=True,
        download_name=combined.name,
    )


def _artifact_named_file(run_dir: Path, *, key: str, patterns: tuple[str, ...]) -> Path | None:
    """Resolve a packaged file from manifest ``key`` or filename patterns."""
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        name = manifest.get(key)
        if name:
            path = run_dir / str(name)
            if path.is_file():
                return path
    for pat in patterns:
        hits = sorted(run_dir.glob(pat), key=lambda p: p.stat().st_mtime, reverse=True)
        if hits:
            return hits[0]
    return None


@app.route("/api/artifacts/<run_id>/modbus")


def api_artifact_modbus(run_id: str):
    """Download the packaged Modbus / SBX log for a completed lab run."""
    config = DriveConfig.from_env()
    try:
        run_dir = resolve_artifact_dir(config.artifacts_dir, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    path = _artifact_named_file(
        run_dir,
        key="modbus_log",
        patterns=("Modbus logs_*.txt", "Modbus_logs_*.txt", "modbus_logs_*.txt"),
    )
    if path is None:
        return jsonify({"ok": False, "error": f"Modbus log not found for {run_id}"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/api/artifacts/<run_id>/pilot")


def api_artifact_pilot(run_id: str):
    """Download the dedicated pilot capture JSONL for a completed lab run."""
    config = DriveConfig.from_env()
    try:
        run_dir = resolve_artifact_dir(config.artifacts_dir, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    path = _artifact_named_file(
        run_dir,
        key="pilot_log",
        patterns=(f"{run_id}_pilot.jsonl", "*_pilot.jsonl"),
    )
    if path is None:
        return jsonify({"ok": False, "error": f"Pilot log not found for {run_id}"}), 404
    return send_file(
        path,
        mimetype="application/x-ndjson",
        as_attachment=True,
        download_name=path.name,
    )


@app.route("/api/artifacts/<run_id>/ev_loads")


def api_artifact_ev_loads(run_id: str):
    config = DriveConfig.from_env()
    try:
        run_dir = resolve_artifact_dir(config.artifacts_dir, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    path = _artifact_named_file(
        run_dir,
        key="ev_loads_log",
        patterns=(f"{run_id}_ev_loads.jsonl", "*_ev_loads.jsonl"),
    )
    if path is None:
        return jsonify({"ok": False, "error": f"EV loads log not found for {run_id}"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/api/artifacts/<run_id>/lb2")


def api_artifact_lb2(run_id: str):
    config = DriveConfig.from_env()
    try:
        run_dir = resolve_artifact_dir(config.artifacts_dir, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    path = _artifact_named_file(
        run_dir,
        key="lb2_log",
        patterns=(f"{run_id}_lb2.jsonl", "*_lb2.jsonl"),
    )
    if path is None:
        return jsonify({"ok": False, "error": f"LB2 log not found for {run_id}"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/api/artifacts/<run_id>/panel")


def api_artifact_panel(run_id: str):
    config = DriveConfig.from_env()
    try:
        run_dir = resolve_artifact_dir(config.artifacts_dir, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    path = _artifact_named_file(
        run_dir,
        key="panel_log",
        patterns=(f"{run_id}_panel.jsonl", "*_panel.jsonl"),
    )
    if path is None:
        return jsonify({"ok": False, "error": f"Panel log not found for {run_id}"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/api/artifacts/<run_id>/report")


def api_artifact_report(run_id: str):
    """Serve the JAMA-style HTML run report for a completed lab run."""
    config = DriveConfig.from_env()
    try:
        run_dir = resolve_artifact_dir(config.artifacts_dir, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    report = run_dir / "drive_report.html"
    if not report.is_file():
        return jsonify({"ok": False, "error": f"Report not found for {run_id}"}), 404

    return send_file(report, mimetype="text/html")


@app.route("/api/artifacts/<run_id>")


def api_artifact_info(run_id: str):
    config = DriveConfig.from_env()
    try:
        run_dir = resolve_artifact_dir(config.artifacts_dir, run_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return jsonify({"ok": False, "error": f"Artifact not found for {run_id}"}), 404

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify(
        {
            "ok": True,
            "run_id": run_id,
            "download_url": f"/api/artifacts/{run_id}/download",
            "modbus_url": f"/api/artifacts/{run_id}/modbus",
            "pilot_url": f"/api/artifacts/{run_id}/pilot",
            "report_url": f"/api/artifacts/{run_id}/report",
            "manifest": manifest,
        }
    )

if __name__ == "__main__":
    import os

    port = int(os.environ.get("DRIVE_DASHBOARD_PORT", "8765"))
    # Debug only. systemd / run_dashboard.sh uses gunicorn (like lucid + uvicorn).
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
