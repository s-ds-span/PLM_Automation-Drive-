"""Scan SBX/Modbus capture logs for commissioning and cyclic handshake messages."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

EmitFn = Callable[..., None]


def _noop_emit(event: str, **payload: Any) -> None:
    return None


_MSG_ID_RE = re.compile(
    r"(?:0x([0-9A-Fa-f]{1,4})|Msg[A-Za-z0-9]+)\s*(?:\((?:0x)?([0-9A-Fa-f]{1,4})\))?",
    re.I,
)
_HEX_IN_DESC = re.compile(r"\(0x([0-9A-Fa-f]{1,4})\)")
_NAMED = {
    "MsgAssignDynamicBusId": 0x2000,
    "MsgAssignDynamicBusIdBroadcast": 0x2000,
    "MsgAck": 0xF000,
    "MsgDriveStartupRequest": 0x100F,
    "MsgDriveStartup": 0x1010,
    "MsgHeartbeatRequest": 0x0001,
    "MsgHeartbeat": 0x0001,
    "MsgFirmwareVersionRequest": 0x0005,
    "MsgFirmwareVersion": 0x0006,
    "MsgGoldenOrActiveImgVersionRequest": 0x0013,
    "MsgGoldenOrActiveImgVersion": 0x0014,
    "MsgExtFlashImgVersionRequest": 0x0015,
    "MsgExtFlashImgVersion": 0x0016,
    "MsgCpuUtilRequest": 0x000D,
    "MsgCpuUtil": 0x000E,
    "MsgCommStatusRequest": 0x0011,
    "MsgCommStatus": 0x0012,
    "MsgDriveFaultCountRequest": 0x100B,
    "MsgFaultCount": 0x100C,
}


@dataclass
class MsgHit:
    msg_id: int
    ts: str = ""
    snippet: str = ""
    line_no: int = 0


@dataclass
class PairResult:
    name: str
    req_id: int
    resp_id: int
    observed: bool = False
    req: Optional[MsgHit] = None
    resp: Optional[MsgHit] = None

    def to_dict(self) -> dict[str, Any]:
        req_snip = (self.req.snippet if self.req else "")[:400]
        resp_snip = (self.resp.snippet if self.resp else "")[:400]
        return {
            "name": self.name,
            "req_id": f"0x{self.req_id:04X}",
            "resp_id": f"0x{self.resp_id:04X}",
            "observed": self.observed,
            "req_ts": (self.req.ts if self.req else None),
            "resp_ts": (self.resp.ts if self.resp else None),
            "req_evidence": req_snip or None,
            "resp_evidence": resp_snip or None,
            "evidence": (req_snip or resp_snip)[:400],
        }


@dataclass
class ScanState:
    hits: dict[int, MsgHit] = field(default_factory=dict)
    offset: int = 0


def resolve_modbus_log(session_dir: Optional[Path]) -> Optional[Path]:
    if session_dir is None:
        return None
    root = Path(session_dir)
    if not root.is_dir():
        return None
    paths = list(root.rglob("Modbus logs_*.txt")) + list(root.rglob("modbus_logs_*.txt"))
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def _parse_msg_ids(line: str) -> list[int]:
    found: list[int] = []
    for name, mid in _NAMED.items():
        if name in line:
            if mid not in found:
                found.append(mid)
    for m in _HEX_IN_DESC.finditer(line):
        mid = int(m.group(1), 16)
        if mid not in found:
            found.append(mid)
    # Raw hex frame often embeds type near the end — also match "0x2000" style.
    for m in re.finditer(r"\b0x([0-9A-Fa-f]{1,4})\b", line):
        mid = int(m.group(1), 16)
        if mid not in found:
            found.append(mid)
    return found


def _line_ts(line: str) -> str:
    part = line.split("|", 1)[0].strip()
    return part[:32] if part else ""


def scan_new_lines(path: Path, state: ScanState) -> list[MsgHit]:
    """Read new bytes from ``path`` since ``state.offset`` and update hits."""
    new_hits: list[MsgHit] = []
    try:
        data = path.read_bytes()
    except OSError:
        return new_hits
    if state.offset > len(data):
        state.offset = 0
    chunk = data[state.offset :]
    if not chunk:
        return new_hits
    text = chunk.decode("utf-8", errors="replace")
    # Drop partial first line when resuming mid-file.
    if state.offset > 0 and not text.startswith("\n") and "\n" in text:
        text = text.split("\n", 1)[-1]
    state.offset = len(data)
    for i, line in enumerate(text.splitlines()):
        if line.startswith("===") or " | " not in line:
            continue
        for mid in _parse_msg_ids(line):
            hit = MsgHit(
                msg_id=mid,
                ts=_line_ts(line),
                snippet=line.strip()[:400],
                line_no=i,
            )
            state.hits[mid] = hit
            new_hits.append(hit)
    return new_hits


def pair_results(
    hits: dict[int, MsgHit],
    pairs: list[tuple[str, int, int]],
) -> list[PairResult]:
    out: list[PairResult] = []
    for name, req_id, resp_id in pairs:
        req = hits.get(req_id)
        resp = hits.get(resp_id)
        # Heartbeat uses same ID both ways — one hit counts as observed.
        if req_id == resp_id:
            observed = req is not None
        else:
            observed = req is not None and resp is not None
        out.append(
            PairResult(
                name=name,
                req_id=req_id,
                resp_id=resp_id,
                observed=observed,
                req=req,
                resp=resp,
            )
        )
    return out


POST_COMMISSION_PAIRS: list[tuple[str, int, int]] = [
    ("AssignDynamicBusId + Ack", 0x2000, 0xF000),
    ("DriveStartupRequest + DriveStartup", 0x100F, 0x1010),
]

HANDSHAKE_CYCLIC_PAIRS: list[tuple[str, int, int]] = [
    ("Heartbeat / MsgAck", 0x0001, 0x0001),
    ("MsgFirmwareVersion", 0x0005, 0x0006),
    ("MsgGoldenOrActiveImgVersion", 0x0013, 0x0014),
    ("MsgExtFlashImgVersion", 0x0015, 0x0016),
    ("MsgCpuUtil", 0x000D, 0x000E),
    ("MsgCommStatus", 0x0011, 0x0012),
    ("MsgFaultCount", 0x100B, 0x100C),
]


def watch_pairs(
    session_dir: Optional[Path],
    pairs: list[tuple[str, int, int]],
    *,
    window_sec: float,
    stop_check: Optional[Callable[[], bool]] = None,
    emit: EmitFn = _noop_emit,
    event_name: str = "sbx_msg_verify",
    poll_sec: float = 1.0,
) -> list[dict[str, Any]]:
    """Watch Modbus log for ``window_sec`` and report each pair observed / not."""
    path = resolve_modbus_log(session_dir)
    state = ScanState()
    if path is not None and path.is_file():
        # Seed from existing content so messages during commission are counted.
        try:
            state.offset = 0
            scan_new_lines(path, state)
        except Exception:
            pass

    deadline = time.monotonic() + float(window_sec)
    emit(
        event_name + "_start",
        window_sec=window_sec,
        modbus_log=str(path) if path else None,
        message=f"Watching SBX Modbus log for {window_sec:.0f}s",
    )
    while time.monotonic() < deadline:
        if stop_check and stop_check():
            break
        if path is None:
            path = resolve_modbus_log(session_dir)
        if path is not None and path.is_file():
            scan_new_lines(path, state)
        # Early exit when all pairs observed.
        results = pair_results(state.hits, pairs)
        if all(r.observed for r in results):
            break
        time.sleep(poll_sec)

    if path is not None and path.is_file():
        scan_new_lines(path, state)
    results = pair_results(state.hits, pairs)
    payload = [r.to_dict() for r in results]
    for row in payload:
        status = "observed" if row["observed"] else "not observed"
        emit(
            event_name,
            **row,
            message=f"{row['name']}: {status}",
        )
    emit(
        event_name + "_summary",
        pairs=payload,
        all_observed=all(r["observed"] for r in payload),
        message=(
            "All expected SBX messages observed"
            if all(r["observed"] for r in payload)
            else "Some SBX messages not observed — continuing"
        ),
    )
    return payload


# --- Pipeline-wide SBX abnormality monitoring (fault_mask, UNDECODED, etc.) ---

_FAULT_MASK_RE = re.compile(r"fault_mask=(0x[0-9A-Fa-f]+)")
_CHARGING_STATE_RE = re.compile(r"charging_state=([A-Z0-9_]+)")
_RESET_CAUSE_RE = re.compile(r"reset_cause=(\d+)")


@dataclass
class AbnormalityState:
    """Incremental Modbus log scan state for live abnormality detection."""

    offset: int = 0
    seen_keys: set[str] = field(default_factory=set)
    last_fault_mask: str = ""
    last_charging_state: str = ""


def _abnormality_key(kind: str, ts: str, detail: str) -> str:
    return f"{kind}|{ts}|{detail[:120]}"


def detect_line_abnormalities(line: str) -> list[dict[str, Any]]:
    """Return zero or more abnormality dicts for a single Modbus log line."""
    if line.startswith("===") or " | " not in line:
        return []
    ts = _line_ts(line)
    snippet = line.strip()[:400]
    out: list[dict[str, Any]] = []

    if "UNDECODED" in line:
        out.append({
            "kind": "undecoded_bytes",
            "ts": ts,
            "message": "Undecoded SBX bytes on the wire",
            "evidence": snippet,
        })

    m_fault = _FAULT_MASK_RE.search(line)
    if m_fault:
        mask = m_fault.group(1).lower()
        if mask not in ("0x00000000", "0x0"):
            out.append({
                "kind": "fault_mask",
                "ts": ts,
                "fault_mask": mask,
                "message": f"Drive fault_mask={mask} in MsgDriveStatus",
                "evidence": snippet,
            })

    m_state = _CHARGING_STATE_RE.search(line)
    if m_state:
        state = m_state.group(1)
        if "FAULT" in state.upper():
            out.append({
                "kind": "charging_fault_state",
                "ts": ts,
                "charging_state": state,
                "message": f"charging_state={state}",
                "evidence": snippet,
            })

    if "MsgDriveStartup" in line and "reset_cause=" in line:
        m_rc = _RESET_CAUSE_RE.search(line)
        rc = m_rc.group(1) if m_rc else ""
        # reset_cause=1 is the normal periodic startup poll; flag unusual values only.
        try:
            rc_n = int(rc)
        except (TypeError, ValueError):
            rc_n = -1
        if rc_n > 1:
            out.append({
                "kind": "cp_reset",
                "ts": ts,
                "reset_cause": rc,
                "message": f"Charge point reset (MsgDriveStartup reset_cause={rc})",
                "evidence": snippet,
            })

    return out


def scan_abnormalities(
    path: Path,
    state: AbnormalityState,
    *,
    dedupe: bool = True,
) -> list[dict[str, Any]]:
    """Read new bytes from ``path`` and return newly detected abnormalities."""
    new: list[dict[str, Any]] = []
    try:
        data = path.read_bytes()
    except OSError:
        return new
    if state.offset > len(data):
        state.offset = 0
    chunk = data[state.offset :]
    if not chunk:
        return new
    text = chunk.decode("utf-8", errors="replace")
    if state.offset > 0 and not text.startswith("\n") and "\n" in text:
        text = text.split("\n", 1)[-1]
    state.offset = len(data)

    for line in text.splitlines():
        for hit in detect_line_abnormalities(line):
            kind = str(hit.get("kind") or "")
            if kind == "fault_mask":
                mask = str(hit.get("fault_mask") or "")
                if mask == state.last_fault_mask:
                    continue
                state.last_fault_mask = mask
            elif kind == "charging_fault_state":
                st = str(hit.get("charging_state") or "")
                if st == state.last_charging_state:
                    continue
                state.last_charging_state = st
            key = _abnormality_key(kind, str(hit.get("ts") or ""), str(hit.get("message") or ""))
            if dedupe and key in state.seen_keys:
                continue
            if dedupe:
                state.seen_keys.add(key)
            new.append(hit)
    return new


def scan_modbus_log_abnormalities(path: Path) -> list[dict[str, Any]]:
    """Full-file scan (used at end of run for report completeness)."""
    state = AbnormalityState()
    scan_abnormalities(path, state, dedupe=True)
    # Re-scan entire file without offset skip for complete pass.
    state.offset = 0
    state.seen_keys.clear()
    state.last_fault_mask = ""
    state.last_charging_state = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    hits: list[dict[str, Any]] = []
    for line in text.splitlines():
        for hit in detect_line_abnormalities(line):
            kind = str(hit.get("kind") or "")
            if kind == "fault_mask":
                mask = str(hit.get("fault_mask") or "")
                if mask == state.last_fault_mask:
                    continue
                state.last_fault_mask = mask
            elif kind == "charging_fault_state":
                st = str(hit.get("charging_state") or "")
                if st == state.last_charging_state:
                    continue
                state.last_charging_state = st
            key = _abnormality_key(kind, str(hit.get("ts") or ""), str(hit.get("message") or ""))
            if key in state.seen_keys:
                continue
            state.seen_keys.add(key)
            hits.append(hit)
    return hits


ContextFn = Callable[[], dict[str, Any]]


def watch_abnormalities(
    session_dir: Optional[Path],
    *,
    stop_check: Optional[Callable[[], bool]] = None,
    emit: EmitFn = _noop_emit,
    get_context: Optional[ContextFn] = None,
    poll_sec: float = 2.0,
    hits_out: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Poll Modbus log during the pipeline and emit ``sbx_abnormality`` events."""
    path = resolve_modbus_log(session_dir)
    state = AbnormalityState()
    collected: list[dict[str, Any]] = hits_out if hits_out is not None else []

    emit(
        "sbx_abnormality_watch_start",
        modbus_log=str(path) if path else None,
        message="SBX abnormality monitor started (fault_mask, UNDECODED, CP reset, …)",
    )

    while True:
        if stop_check and stop_check():
            break
        if path is None:
            path = resolve_modbus_log(session_dir)
        if path is not None and path.is_file():
            for hit in scan_abnormalities(path, state):
                ctx = get_context() if get_context else {}
                row = {**hit, **ctx}
                collected.append(row)
                emit(
                    "sbx_abnormality",
                    **row,
                    message=row.get("message") or "SBX abnormality",
                )
        time.sleep(poll_sec)

    if path is None:
        path = resolve_modbus_log(session_dir)
    if path is not None and path.is_file():
        for hit in scan_abnormalities(path, state):
            ctx = get_context() if get_context else {}
            row = {**hit, **ctx}
            key = _abnormality_key(
                str(row.get("kind") or ""),
                str(row.get("ts") or ""),
                str(row.get("message") or ""),
            )
            if key in state.seen_keys:
                continue
            state.seen_keys.add(key)
            collected.append(row)
            emit("sbx_abnormality", **row, message=row.get("message") or "SBX abnormality")

    return collected
