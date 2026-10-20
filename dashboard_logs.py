"""Dashboard helpers: unified CAN/SBX capture, UART logger, live tails.

Drive variant of lucid ``dashboard_logs.py``:
  • Unified backend: ``unified_capture.run_capture`` (CAN + SBX serial)
  • UART backend: ``uart_capture.UartCapture`` (gateway journal / serial)
  • Modbus widget shows latest Drive SBX status (charge_rate / charging_state)
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent


def capture_sessions_root() -> Path:
    raw = (os.environ.get("DRIVE_CAPTURE_DIR") or "").strip()
    if raw:
        root = Path(os.path.expanduser(os.path.expandvars(raw))).resolve()
    else:
        root = _PROJECT_ROOT / "capture_sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


_SESSION_ROOT = capture_sessions_root()

_unified_lock = threading.Lock()
_unified_state: dict[str, Any] = {
    "running": False,
    "thread": None,
    "stop": None,
    "session_id": None,
    "session_dir": None,
    "error": None,
    "warnings": [],
    "can_ok": None,
    "modbus_ok": None,
    "skip_can": False,
    "host": None,
    "username": None,
    "started_at": None,
    "capture_files": {},
}

_sticky_relay_cache: dict[str, dict[str, str]] = {}

_uart_lock = threading.Lock()
_uart_logger: Any = None
# Allowed directories for UART log downloads (retained after stop).
_uart_roots: set[Path] = set()
_uart_last_snapshot: dict[str, Any] = dict()

_RELAY_SWITCH_RE = re.compile(
    r'"(?:switch_state|mid_relay_state)"\s*:\s*"(SWITCH_STATE_[^"]+)"'
)
# Panel current from CAN bus (same decode as live_panel_current_monitor.py):
# total panel current = MAINSMID leg L1N current + leg L2N current.
_CAN_TS_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?::\d{3})? \| ")
_RMS_RE = re.compile(
    r'src=(?P<src>\d+)\s+dst=\d+\s+broadcast=\w+\s+'
    r'type=PAYLOAD_STPM_RMS_MEASUREMENTS\s+value=\{"StpmRmsMeasurements":\s*\{'
    r'"dt_millisecs":\s*\d+,\s*"channel":\s*\d+,\s*'
    r'"line":\s*"(?P<line>[^"]+)",\s*"current_rms_milliamps":\s*(?P<current_ma>\d+)'
)
_DEVICE_INFO_RE = re.compile(
    r'type=PAYLOAD_DEVICE_INFO\s+value=\{"DeviceInfo":\s*\{'
    r'"address":\s*(?P<address>\d+),\s*"module_type":\s*"(?P<module_type>[^"]+)"'
)
# MAINSMID module address per session (device-info frames may scroll out of the
# tail window, so remember the detected address once we've seen it).
_mains_addr_cache: dict[str, int] = {}
_DRIVE_STATUS_RE = re.compile(
    r"MsgDriveStatus.*?charging_state=([A-Z0-9_]+).*?charge_rate_a=(\d+)",
    re.IGNORECASE,
)
_DRIVE_STATUS_ALT_RE = re.compile(
    r"charging_state=([A-Z0-9_]+).*charge_rate_a=(\d+)",
    re.IGNORECASE,
)
# Extra fields on MsgDriveStatus when primary charge_rate_a is absent / late.
_CHARGE_RATE_ANY_RE = re.compile(
    r"(?:requested_charge_rate_a|observed_charge_rate_a|rotary_max_charge_rate_a|"
    r"charge_rate_a)=(\d+)",
    re.IGNORECASE,
)
_OBSERVED_CHARGE_RATE_RE = re.compile(
    r"observed_charge_rate_a=(\d+)",
    re.IGNORECASE,
)


def _observed_from_line(line: str) -> Optional[int]:
    m = _OBSERVED_CHARGE_RATE_RE.search(line)
    return int(m.group(1)) if m else None


def new_capture_session_id(prefix: str = "") -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{prefix}{stamp}_{suffix}"


def notify_unified_capture_files(
    *,
    session_dir: str,
    can_log: str,
    can_capture: str,
    modbus_log: str = "",
    modbus_capture: str = "",
) -> None:
    with _unified_lock:
        _unified_state["capture_files"] = {
            "session_dir": session_dir,
            "can_log": can_log,
            "can_capture": can_capture,
            "modbus_log": modbus_log,
            "modbus_capture": modbus_capture,
        }


def notify_unified_capture_health(
    *,
    can_ok: Optional[bool] = None,
    modbus_ok: Optional[bool] = None,
    warnings: Optional[list[str]] = None,
    error: Optional[str] = None,
) -> None:
    """Update CAN/SBX health flags and user-visible warnings during capture."""
    with _unified_lock:
        if can_ok is not None:
            _unified_state["can_ok"] = can_ok
        if modbus_ok is not None:
            _unified_state["modbus_ok"] = modbus_ok
        if warnings is not None:
            _unified_state["warnings"] = list(warnings)
        if error is not None:
            _unified_state["error"] = error


def _switch_display(raw: str) -> str:
    r = raw.upper()
    if "CLOSED" in r:
        return "CLOSED"
    if "OPEN" in r:
        return "OPEN"
    return raw.replace("SWITCH_STATE_", "").replace("_", " ")


def _read_file_tail(path: Path, max_chars: int) -> str:
    try:
        size = path.stat().st_size
        if size <= 0:
            return ""
        with path.open("rb") as fh:
            fh.seek(max(0, size - max_chars))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _read_file_tail_lines(path: Path, max_lines: int) -> tuple[str, int]:
    text = _read_file_tail(path, max(12000, max_lines * 500))
    if not text:
        return "", 0
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines), len(lines)


def _latest_file(root: Path, pattern: str) -> Optional[Path]:
    paths = list(root.rglob(pattern))
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def parse_can_relays_from_text(text: str) -> dict[str, str]:
    out: dict[str, str | None] = {
        "nft_relay": None,
        "der_relay": None,
        "mid_relay": None,
    }
    for line in text.splitlines():
        if "PAYLOAD_MID_RELAY_STATE" in line:
            m = _RELAY_SWITCH_RE.search(line)
            if m:
                out["mid_relay"] = _switch_display(m.group(1))
        elif "PAYLOAD_DER_LANDED_CIRCUIT_CONTROL_STATE" in line:
            m = _RELAY_SWITCH_RE.search(line)
            if m:
                out["der_relay"] = _switch_display(m.group(1))
        elif "PAYLOAD_NFT_LANDED_CIRCUIT_CONTROL_STATE" in line:
            m = _RELAY_SWITCH_RE.search(line)
            if m:
                out["nft_relay"] = _switch_display(m.group(1))
    return {
        "nft_relay": out["nft_relay"] or "—",
        "der_relay": out["der_relay"] or "—",
        "mid_relay": out["mid_relay"] or "—",
    }


def parse_drive_status_from_sbx_text(text: str) -> str:
    """Latest Drive status summary from SBX/Modbus log lines."""
    last = "—"
    for line in text.splitlines():
        if "MsgDriveStatus" not in line and "charging_state=" not in line:
            continue
        m = _DRIVE_STATUS_RE.search(line) or _DRIVE_STATUS_ALT_RE.search(line)
        if m:
            last = f"{m.group(1)} @ {m.group(2)} A"
        elif "MsgSetChargeRate" in line and "charge_rate_a=" in line:
            m2 = re.search(r"charge_rate_a=(\d+)", line)
            if m2:
                last = f"SetChargeRate {m2.group(1)} A"
    return last


def active_unified_session_dir() -> Optional[Path]:
    """Session dir of the currently running unified capture, if any."""
    with _unified_lock:
        if not _unified_state.get("running"):
            return None
        raw = _unified_state.get("session_dir")
    return Path(raw) if raw else None


_FAULT_MASK_RE = re.compile(r"fault_mask=(0x[0-9A-Fa-f]+)", re.IGNORECASE)
_CHARGING_STATE_LINE_RE = re.compile(r"charging_state=([A-Z0-9_]+)", re.IGNORECASE)
# Modbus log timestamps look like ``2026-09-03 10:45:40:383`` (ms after last colon).
_MODBUS_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2}):(\d{2})[:.](\d{1,3})"
)
# Prefer a MsgDriveStatus with observed over a MsgSetChargeRate that lands only
# ~1s later (panel often re-sends set-rate after status).
_STATUS_OVER_SET_MAX_AGE_SEC = 5.0


def _fault_mask_from_line(line: str) -> Optional[str]:
    m = _FAULT_MASK_RE.search(line)
    return m.group(1).lower() if m else None


def _charging_state_from_line(line: str) -> Optional[str]:
    m = _CHARGING_STATE_LINE_RE.search(line)
    return m.group(1) if m else None


def _modbus_ts_seconds(ts: str) -> Optional[float]:
    """Parse Modbus log timestamp to epoch seconds (UTC-naive local wall clock)."""
    if not ts:
        return None
    m = _MODBUS_TS_RE.search(ts)
    if not m:
        return None
    try:
        from datetime import datetime

        y, mo, d = m.group(1).split("-")
        ms = m.group(5).ljust(3, "0")[:3]
        dt = datetime(
            int(y),
            int(mo),
            int(d),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4)),
            int(ms) * 1000,
        )
        return dt.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _enrich_drive_fields(row: dict[str, Any], line: str) -> dict[str, Any]:
    """Attach fault_mask / charging_state when present on the Modbus line."""
    mask = _fault_mask_from_line(line)
    if mask is not None:
        row["fault_mask"] = mask
    state = _charging_state_from_line(line)
    if state is not None:
        row["charging_state"] = state
    return row


def _pick_charge_rate_detail(
    last_status: Optional[dict[str, Any]],
    last_set: Optional[dict[str, Any]],
    *,
    last_status_i: int,
    last_set_i: int,
    max_age_sec: float = _STATUS_OVER_SET_MAX_AGE_SEC,
) -> Optional[dict[str, Any]]:
    """Choose DriveStatus vs SetChargeRate for JAMA metrics.

    Panel often emits ``MsgSetChargeRate`` then, ~1s later, ``MsgDriveStatus``
    with ``observed_charge_rate_a``, then another SetChargeRate. Blindly taking
    the chronologically newest line drops observed and cites the wrong evidence.
    Prefer a recent DriveStatus that carries observed (or is newest).
    """
    if last_status is None:
        return last_set
    if last_set is None:
        return last_status
    if last_status_i >= last_set_i:
        return last_status

    # SetChargeRate is newer in the file — still prefer status when it is fresh
    # and has observed (typical: status @ :383, set @ :527 same second).
    status_obs = last_status.get("observed_amps")
    t_status = _modbus_ts_seconds(str(last_status.get("ts") or ""))
    t_set = _modbus_ts_seconds(str(last_set.get("ts") or ""))
    if status_obs is not None and t_status is not None and t_set is not None:
        age = t_set - t_status
        if 0.0 <= age <= float(max_age_sec):
            return last_status
    if status_obs is not None and (t_status is None or t_set is None):
        # Timestamps missing — prefer status with observed over bare set-rate.
        return last_status
    return last_set


def latest_sbx_charge_rate_detail(session_dir: Path) -> Optional[dict[str, Any]]:
    """Latest panel->drive charge_rate with the Modbus log line that proved it.

    Prefers a recent ``MsgDriveStatus`` (with ``observed_charge_rate_a`` when
    present) over a slightly newer ``MsgSetChargeRate`` so JAMA evidence is the
    status frame, not the set-rate command. Falls back to SetChargeRate only
    when no usable DriveStatus is in the tail.

    Keys: ``amps``, ``observed_amps`` (optional), ``fault_mask``, ``charging_state``,
    ``evidence``, ``source``, ``modbus_log``, ``ts``.
    """
    mod_tail = int(os.environ.get("UNIFIED_LOG_TAIL_CHARS", "120000"))
    mod_path = _latest_file(session_dir, "Modbus logs_*.txt")
    if mod_path is None:
        mod_path = _latest_file(session_dir, "modbus_logs_*.txt")
    if mod_path is None or not mod_path.is_file():
        return None
    text = _read_file_tail(mod_path, mod_tail)
    last_status: Optional[dict[str, Any]] = None
    last_set: Optional[dict[str, Any]] = None
    last_status_i = -1
    last_set_i = -1
    for i, line in enumerate(text.splitlines()):
        raw = line.strip()
        if not raw or raw.startswith("==="):
            continue
        ts_part = line.split("|", 1)[0].strip()[:32]
        m = _DRIVE_STATUS_RE.search(line) or _DRIVE_STATUS_ALT_RE.search(line)
        if m:
            last_status = _enrich_drive_fields(
                {
                    "amps": int(m.group(2)),
                    "observed_amps": _observed_from_line(line),
                    "evidence": raw[:400],
                    "source": "MsgDriveStatus",
                    "modbus_log": str(mod_path),
                    "ts": ts_part,
                },
                line,
            )
            last_status_i = i
            continue
        if "MsgSetChargeRate" in line and "charge_rate_a=" in line:
            m2 = re.search(r"charge_rate_a=(\d+)", line)
            if m2:
                last_set = {
                    "amps": int(m2.group(1)),
                    "observed_amps": _observed_from_line(line),
                    "evidence": raw[:400],
                    "source": "MsgSetChargeRate",
                    "modbus_log": str(mod_path),
                    "ts": ts_part,
                }
                last_set_i = i
                continue
        # Late / alternate field names on DriveStatus lines.
        if "MsgDriveStatus" in line or "DriveStatus" in line:
            m3 = _CHARGE_RATE_ANY_RE.search(line)
            if m3:
                amps_m = re.search(r"(?<![a-z_])charge_rate_a=(\d+)", line, re.I)
                if amps_m is None:
                    amps_m = re.search(r"charge_rate_a=(\d+)", line, re.I)
                amps_val = int(amps_m.group(1)) if amps_m else int(m3.group(1))
                last_status = _enrich_drive_fields(
                    {
                        "amps": amps_val,
                        "observed_amps": _observed_from_line(line),
                        "evidence": raw[:400],
                        "source": "MsgDriveStatus(alt)",
                        "modbus_log": str(mod_path),
                        "ts": ts_part,
                    },
                    line,
                )
                last_status_i = i
    return _pick_charge_rate_detail(
        last_status,
        last_set,
        last_status_i=last_status_i,
        last_set_i=last_set_i,
    )


def latest_sbx_fault_detail(session_dir: Path) -> Optional[dict[str, Any]]:
    """Latest non-zero fault_mask / FAULT charging_state from a MsgDriveStatus line."""
    mod_tail = int(os.environ.get("UNIFIED_LOG_TAIL_CHARS", "120000"))
    mod_path = _latest_file(session_dir, "Modbus logs_*.txt")
    if mod_path is None:
        mod_path = _latest_file(session_dir, "modbus_logs_*.txt")
    if mod_path is None or not mod_path.is_file():
        return None
    text = _read_file_tail(mod_path, mod_tail)
    last_fault: Optional[dict[str, Any]] = None
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("==="):
            continue
        if "MsgDriveStatus" not in line and "DriveStatus" not in line:
            continue
        mask = _fault_mask_from_line(line)
        state = _charging_state_from_line(line) or ""
        active_mask = bool(mask) and mask not in ("0x00000000", "0x0")
        active_state = "FAULT" in state.upper()
        if not active_mask and not active_state:
            continue
        ts_part = line.split("|", 1)[0].strip()[:32]
        last_fault = {
            "fault_mask": mask if active_mask else None,
            "charging_state": state or None,
            "evidence": raw[:400],
            "ts": ts_part,
            "source": "MsgDriveStatus",
            "modbus_log": str(mod_path),
            "observed_amps": _observed_from_line(line),
            "active": True,
        }
    return last_fault


def latest_sbx_charge_rate_a(session_dir: Path) -> Optional[int]:
    """Latest panel->drive commanded current (amps) from the running SBX log.

    Prefers ``MsgDriveStatus.charge_rate_a`` (value in effect); falls back to
    ``MsgSetChargeRate.charge_rate_a`` (explicit set command). Returns None when
    no charge rate has been observed yet.
    """
    detail = latest_sbx_charge_rate_detail(session_dir)
    if detail is None:
        return None
    return int(detail["amps"])


def sbx_modbus_has_frames(session_dir: Path) -> bool:
    """True when Modbus log has at least one decoded SBX frame line (not header-only)."""
    mod_path = _latest_file(session_dir, "Modbus logs_*.txt")
    if mod_path is None:
        mod_path = _latest_file(session_dir, "modbus_logs_*.txt")
    if mod_path is None or not mod_path.is_file():
        return False
    try:
        text = _read_file_tail(mod_path, 8000)
    except OSError:
        return False
    for line in text.splitlines():
        if line.startswith("===") or not line.strip():
            continue
        if " | " in line:
            return True
    return False


def _detect_mains_address(text: str) -> Optional[int]:
    """Return MAINSMID module address from a PAYLOAD_DEVICE_INFO frame, if present."""
    for line in text.splitlines():
        m = _DEVICE_INFO_RE.search(line)
        if m and m.group("module_type") == "MODULE_TYPE_MAINSMID":
            return int(m.group("address"))
    return None


def parse_panel_current_from_can_text(
    text: str, mains_address: Optional[int]
) -> Optional[dict[str, Any]]:
    """Latest total panel current (A) from CAN STPM RMS measurements.

    Mirrors ``live_panel_current_monitor.py``: pair each MAINSMID L1N reading
    with the following L2N reading and sum them. ``mains_address`` filters to the
    MAINSMID module; when None, we take RMS from any module (single-source labs).
    Returns None when no complete L1/L2 pair is present in ``text``.
    """
    pending_l1: Optional[float] = None
    latest: Optional[dict[str, Any]] = None
    want = str(mains_address) if mains_address is not None else None
    for raw in text.splitlines():
        m = _RMS_RE.search(raw)
        if not m:
            continue
        if want is not None and m.group("src") != want:
            continue
        current_a = int(m.group("current_ma")) / 1000.0
        line_ref = m.group("line")
        if "L1N" in line_ref:
            pending_l1 = current_a
        elif "L2N" in line_ref and pending_l1 is not None:
            latest = {
                "l1_a": round(pending_l1, 3),
                "l2_a": round(current_a, 3),
                "total_a": round(pending_l1 + current_a, 3),
            }
            pending_l1 = None
    return latest


def latest_panel_current(session_dir: Path) -> Optional[dict[str, Any]]:
    """Latest total panel current (A) from the running CAN bus log."""
    can_tail = int(os.environ.get("UNIFIED_CAN_TAIL_CHARS", "120000"))
    can_log = _latest_file(session_dir, "CAN bus logs_*.txt")
    if can_log is None or not can_log.is_file():
        return None
    text = _read_file_tail(can_log, can_tail)
    if not text:
        return None

    sess_key = str(session_dir.resolve())
    mains_addr = _mains_addr_cache.get(sess_key)
    if mains_addr is None:
        detected = _detect_mains_address(text)
        if detected is None:
            env_addr = (os.environ.get("DRIVE_MAINS_ADDRESS") or "").strip()
            detected = int(env_addr) if env_addr.isdigit() else None
        if detected is not None:
            mains_addr = detected
            _mains_addr_cache[sess_key] = mains_addr

    pc = parse_panel_current_from_can_text(text, mains_addr)
    if pc is None:
        return None
    pc["mains_address"] = mains_addr
    return pc


def panel_current_now() -> dict[str, Any]:
    """Lightweight latest panel current for the active capture (fast-poll endpoint).

    Only reads the CAN log tail (no relay/SBX parsing) so it can be polled at
    ~1 s without the cost of the full ``unified_status`` snapshot.
    """
    session_dir = active_unified_session_dir()
    if session_dir is None:
        return {"ok": True, "running": False, "panel_current": None}
    return {
        "ok": True,
        "running": True,
        "panel_current": latest_panel_current(session_dir),
    }


def snapshot_from_session_dir(session_dir: Path) -> dict[str, Any]:
    can_tail = int(os.environ.get("UNIFIED_CAN_TAIL_CHARS", "120000"))
    mod_tail = int(os.environ.get("UNIFIED_LOG_TAIL_CHARS", "40000"))
    can_log = _latest_file(session_dir, "CAN bus logs_*.txt")
    can_path = _latest_file(session_dir, "CAN bus capture_*.txt")
    if can_path is None:
        can_path = _latest_file(session_dir, "canbus_capture_*.txt")
    mod_path = _latest_file(session_dir, "Modbus logs_*.txt")
    if mod_path is None:
        mod_path = _latest_file(session_dir, "modbus_logs_*.txt")

    sess_key = str(session_dir.resolve())
    relays = dict(
        _sticky_relay_cache.get(sess_key)
        or {"mid_relay": "—", "der_relay": "—", "nft_relay": "—"}
    )
    for path in (can_log, can_path):
        if path and path.is_file():
            txt = _read_file_tail(path, can_tail)
            parsed = parse_can_relays_from_text(txt)
            for key, val in parsed.items():
                if val and val != "—":
                    relays[key] = val
    _sticky_relay_cache[sess_key] = relays

    mod_txt = _read_file_tail(mod_path, mod_tail) if mod_path else ""
    drive_status = parse_drive_status_from_sbx_text(mod_txt)
    panel_current = latest_panel_current(session_dir)
    return {
        "can_relays": relays,
        "mid_led_mode_request": drive_status,  # UI reuses this field label in Drive as status
        "drive_status": drive_status,
        "panel_current": panel_current,
        "can_log_file": str(can_log) if can_log else None,
        "can_capture_file": str(can_path) if can_path else None,
        "modbus_log_file": str(mod_path) if mod_path else None,
    }


def list_session_files(session_dir: Path) -> list[str]:
    if not session_dir.is_dir():
        return []
    out: list[str] = []
    for p in sorted(session_dir.rglob("*")):
        if p.is_file():
            try:
                out.append(str(p.relative_to(session_dir)))
            except ValueError:
                out.append(p.name)
    return out


def _resolve_capture_log_path(
    *,
    kind_u: str,
    registered: dict[str, Any],
    base: Path,
    cap_user: str,
    cap_host: str,
) -> Path | None:
    host_sub = base / f"{cap_user}@{cap_host}"
    search_roots = [host_sub, base] if host_sub.is_dir() else [base]

    def _pick(*keys: str, patterns: tuple[str, ...]) -> Path | None:
        for key in keys:
            raw = registered.get(key)
            if raw:
                p = Path(str(raw))
                if p.is_file():
                    return p
        for root in search_roots:
            for pat in patterns:
                hit = _latest_file(root, pat)
                if hit and hit.is_file():
                    return hit
        return None

    if kind_u == "can":
        return _pick(
            "can_log",
            "can_capture",
            patterns=("CAN bus logs_*.txt", "canbus_capture_*.txt", "CAN bus capture_*.txt"),
        )
    return _pick(
        "modbus_log",
        "modbus_capture",
        patterns=("Modbus logs_*.txt", "modbus_logs_*.txt", "Modbus capture_*.txt"),
    )


def _latest_log_ts(chunk: str) -> str:
    for line in reversed(chunk.splitlines()):
        # "2026-07-16 11:22:30:123 |" or "2026-07-16 11:22:30 |"
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if m:
            return m.group(1)
    return ""


def unified_capture_tail(
    *,
    kind: str,
    since_byte: int = 0,
    max_lines: int | None = None,
) -> dict[str, Any]:
    kind_u = str(kind or "").strip().lower()
    if kind_u not in ("can", "modbus"):
        return {"ok": False, "error": "kind must be can or modbus", "chunk": ""}
    with _unified_lock:
        running = bool(_unified_state.get("running"))
        registered = dict(_unified_state.get("capture_files") or {})
        sess = _unified_state.get("session_dir")
        sid = _unified_state.get("session_id")
        cap_user = str(_unified_state.get("username") or "")
        cap_host = str(_unified_state.get("host") or "")
    if not sess:
        return {
            "ok": False,
            "error": "No capture session",
            "chunk": "",
            "running": running,
            "kind": kind_u,
        }
    base = Path(str(sess))
    path = _resolve_capture_log_path(
        kind_u=kind_u,
        registered=registered,
        base=base,
        cap_user=cap_user,
        cap_host=cap_host,
    )
    if path is None:
        return {
            "ok": True,
            "running": running,
            "kind": kind_u,
            "chunk": "(waiting for log file…)",
            "file": None,
            "offset": 0,
            "next_byte": 0,
            "session_id": sid,
            "complete": True,
            "tail_mode": "lines" if max_lines else "bytes",
        }
    try:
        rel = str(path.relative_to(base))
    except ValueError:
        rel = path.name
    mtime = ""
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        size = path.stat().st_size
    except OSError:
        size = 0

    if max_lines is not None and max_lines > 0:
        chunk, _n = _read_file_tail_lines(path, max_lines)
        return {
            "ok": True,
            "running": running,
            "kind": kind_u,
            "chunk": chunk,
            "file": rel,
            "offset": size,
            "next_byte": size,
            "session_id": sid,
            "truncate_prefix": False,
            "complete": True,
            "tail_mode": "lines",
            "file_mtime": mtime,
            "latest_log_ts": _latest_log_ts(chunk),
        }

    try:
        with path.open("rb") as fh:
            fh.seek(max(0, int(since_byte)))
            data = fh.read()
        chunk = data.decode("utf-8", errors="replace")
        next_byte = max(0, int(since_byte)) + len(data)
    except OSError as exc:
        return {"ok": False, "error": str(exc), "chunk": "", "kind": kind_u}

    return {
        "ok": True,
        "running": running,
        "kind": kind_u,
        "chunk": chunk,
        "file": rel,
        "offset": size,
        "next_byte": next_byte,
        "session_id": sid,
        "truncate_prefix": False,
        "complete": True,
        "tail_mode": "bytes",
        "file_mtime": mtime,
        "latest_log_ts": _latest_log_ts(chunk),
    }


def unified_status() -> dict[str, Any]:
    with _unified_lock:
        st = dict(_unified_state)
    out: dict[str, Any] = {
        "running": bool(st.get("running")),
        "session_id": st.get("session_id"),
        "session_dir": st.get("session_dir"),
        "host": st.get("host"),
        "username": st.get("username"),
        "error": st.get("error"),
        "warnings": list(st.get("warnings") or []),
        "can_ok": st.get("can_ok"),
        "modbus_ok": st.get("modbus_ok"),
        "skip_can": bool(st.get("skip_can")),
    }
    sess = st.get("session_dir")
    if not sess or not Path(sess).is_dir():
        return out
    snap = snapshot_from_session_dir(Path(sess))
    out.update(snap)
    files = list_session_files(Path(sess))
    out["files"] = files
    registered = dict(st.get("capture_files") or {})
    active: list[str] = []
    base = Path(sess).resolve()
    for key in ("can_log", "can_capture", "modbus_log"):
        raw = registered.get(key)
        if not raw:
            continue
        p = Path(str(raw))
        if p.is_file():
            try:
                active.append(str(p.resolve().relative_to(base)))
            except ValueError:
                active.append(p.name)
    if active and out["running"]:
        out["active_files"] = active
        out["files"] = active
    return out


def start_unified(
    username: str,
    host: str,
    password: str,
    modbus_filters: Optional[list[str]] = None,
    skip_can: bool = False,
) -> dict[str, Any]:
    with _unified_lock:
        if _unified_state["running"]:
            return {"ok": False, "error": "Unified capture already running"}
        try:
            import unified_capture as uc
        except ImportError as exc:
            return {"ok": False, "error": f"unified_capture import failed: {exc}"}

        sid = new_capture_session_id()
        session_path = _SESSION_ROOT / sid
        session_path.mkdir(parents=True, exist_ok=True)
        stop_evt = threading.Event()
        err_box: list[str] = []

        def worker() -> None:
            try:
                uc.run_capture(
                    username=username,
                    host=host,
                    password=password,
                    output_dir=str(session_path),
                    duration=None,
                    stop_requested=stop_evt,
                    modbus_filters=modbus_filters,
                    skip_can=skip_can,
                )
            except Exception as exc:
                err_box.append(f"{type(exc).__name__}: {exc}")
            finally:
                with _unified_lock:
                    _unified_state["running"] = False
                    _unified_state["stop"] = None
                    if err_box:
                        _unified_state["error"] = err_box[0]

        th = threading.Thread(target=worker, name="unified-capture", daemon=True)
        _sticky_relay_cache.pop(str(session_path.resolve()), None)
        _unified_state.update(
            {
                "running": True,
                "thread": th,
                "stop": stop_evt,
                "session_id": sid,
                "session_dir": str(session_path),
                "error": None,
                "warnings": [],
                "can_ok": None,
                "modbus_ok": None,
                "skip_can": bool(skip_can),
                "host": host,
                "username": username,
                "started_at": time.time(),
                "capture_files": {},
            }
        )
        th.start()

        # Drive SBX often lives on the gateway (span-sbx-seriald / eve MQTT),
        # not on a wall FTDI tap. Mirror that into Modbus logs for UI + pipeline.
        def _gw_feed() -> None:
            try:
                from drive_automation.sbx_gateway_feed import run_gateway_sbx_mirror

                run_gateway_sbx_mirror(session_path, stop_evt)
            except Exception as exc:
                with _unified_lock:
                    warnings = list(_unified_state.get("warnings") or [])
                    warnings.append(f"gateway SBX feed: {exc}")
                    _unified_state["warnings"] = warnings

        threading.Thread(target=_gw_feed, name="sbx-gateway-feed", daemon=True).start()
    return {
        "ok": True,
        "session_id": sid,
        "session_dir": str(session_path),
        "skip_can": bool(skip_can),
    }


def stop_unified() -> dict[str, Any]:
    with _unified_lock:
        if not _unified_state["running"] or _unified_state["stop"] is None:
            return {"ok": False, "error": "Unified capture not running"}
        stop = _unified_state["stop"]
        sid = _unified_state.get("session_id")
        stop.set()
    return {"ok": True, "session_id": sid}


def safe_session_download(session_id: str, rel: str) -> Path:
    base = (_SESSION_ROOT / session_id).resolve()
    if not base.is_dir():
        raise FileNotFoundError(f"Unknown session: {session_id}")
    target = (base / rel).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise FileNotFoundError("Invalid download path")
    return target


def _uart_start_for_dashboard(lg: Any, filter_list: list[str]) -> bool:
    """Call ``UARTLogger.start`` in dashboard (non-interactive) mode."""
    sig = inspect.signature(lg.start)
    params = sig.parameters
    if "dashboard" not in params:
        raise RuntimeError(
            "uart_logger_4.py is outdated (missing dashboard= on UARTLogger.start)"
        )
    kw: dict[str, Any] = dict()
    if "serials" in params:
        kw["serials"] = None
    if "filter_strings" in params:
        kw["filter_strings"] = list(filter_list)
    else:
        lg.filter_strings = list(filter_list)
    kw["dashboard"] = True
    return bool(lg.start(**kw))


def start_uart_capture(
    username: str,
    host: str,
    password: str,
    strings: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Start lucid-style UART logger (PCM / MainsMID ST-Link on wall host)."""
    global _uart_logger
    with _uart_lock:
        if _uart_logger is not None:
            return {"ok": False, "error": "UART capture already running"}

    base = str(_SESSION_ROOT / new_capture_session_id(prefix="uart_"))
    Path(base).mkdir(parents=True, exist_ok=True)
    _uart_roots.add(Path(base).resolve())
    filt = list(strings) if strings else []
    done = threading.Event()
    result: dict[str, Any] = {"ok": False, "error": "UART capture did not report status"}

    def runner() -> None:
        global _uart_logger
        try:
            from uart_logger_4 import UARTLogger

            github_base = (os.environ.get("GITHUB_CONFIG_REPO") or "").strip() or None
            lg = UARTLogger(
                host=host,
                username=username,
                password=password,
                output_dir=base,
                github_base=github_base,
            )
            ok = _uart_start_for_dashboard(lg, filt)
            if ok:
                with _uart_lock:
                    _uart_logger = lg
                result["ok"] = True
                result["error"] = None
                result["log_files"] = dict(getattr(lg, "log_files", {}) or {})
                result["capture_files"] = dict(getattr(lg, "capture_files", {}) or {})
            else:
                detail = str(getattr(lg, "last_setup_error", None) or "").strip()
                if detail:
                    result["error"] = f"UART setup failed for {username}@{host}: {detail}"
                else:
                    result["error"] = (
                        f"UART setup failed for {username}@{host} "
                        "(check SSH, sshpass, panels_config.yml, PCM/MainsMID paths)"
                    )
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            done.set()

    threading.Thread(target=runner, name="uart-capture", daemon=True).start()
    if not done.wait(timeout=120.0):
        return {
            "ok": False,
            "error": "UART capture start timed out after 120s",
            "output_dir": base,
        }
    if not result.get("ok"):
        return {
            "ok": False,
            "error": str(result.get("error") or "UART capture failed"),
            "output_dir": base,
        }
    return {
        "ok": True,
        "output_dir": base,
        "log_files": result.get("log_files", {}),
        "capture_files": result.get("capture_files", {}),
    }


def stop_uart_capture() -> dict[str, Any]:
    global _uart_logger
    global _uart_last_snapshot
    with _uart_lock:
        if _uart_logger is None:
            return {"ok": False, "error": "UART capture not running"}
        lg = _uart_logger
    lg.stop()
    _uart_last_snapshot = {
        "output_dir": getattr(lg, "output_dir", None),
        "log_files": getattr(lg, "log_files", {}) or {},
        "capture_files": getattr(lg, "capture_files", {}) or {},
    }
    with _uart_lock:
        _uart_logger = None
    return {"ok": True}


def uart_status() -> dict[str, Any]:
    with _uart_lock:
        on = _uart_logger is not None
        inst = _uart_logger
    out: dict[str, Any] = {"running": on}
    if inst is not None:
        out["output_dir"] = getattr(inst, "output_dir", None)
        out["log_files"] = getattr(inst, "log_files", {}) or {}
        out["capture_files"] = getattr(inst, "capture_files", {}) or {}
        out["host"] = getattr(inst, "host", None)
        out["username"] = getattr(inst, "username", None)
    else:
        out["output_dir"] = _uart_last_snapshot.get("output_dir")
        out["log_files"] = _uart_last_snapshot.get("log_files", {})
        out["capture_files"] = _uart_last_snapshot.get("capture_files", {})
    return out


def uart_resolve_existing_file(path: str) -> Path:
    target = Path(path).expanduser().resolve()
    for root in _uart_roots:
        try:
            if str(target).startswith(str(root.resolve())) and target.is_file():
                return target
        except OSError:
            continue
    raise FileNotFoundError("UART path not allowed or missing")
