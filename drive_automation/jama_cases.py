"""JAMA-style test-case events for the Drive one-click HTML report."""

from __future__ import annotations

from typing import Any, Callable, Optional

from drive_automation.timeutil import now_local

EmitFn = Callable[..., None]


class CaseIdFactory:
    """Allocate sequential DRIVE-<PREFIX>-#### ids within a run."""

    def __init__(self) -> None:
        self._counters: dict[str, int] = dict()

    def next(self, prefix: str) -> str:
        key = str(prefix).upper()
        self._counters[key] = int(self._counters.get(key) or 0) + 1
        return f"DRIVE-{key}-{self._counters[key]:04d}"


def emit_jama_case(
    emit: EmitFn,
    *,
    case_id: str,
    section: str,
    title: str,
    signal: str,
    expected: str,
    observed: str,
    sources: str | list[str],
    result: str,
    evidence: Optional[list[dict[str, Any]]] = None,
    show_evidence: bool = True,
    metrics: Optional[dict[str, Any]] = None,
    message: str = "",
    **extra: Any,
) -> None:
    """Emit a single JAMA-style case used by ``drive_report.html``."""
    src = sources
    if isinstance(src, (list, tuple)):
        src = ", ".join(str(s) for s in src)
    case_ts = str(extra.pop("ts", "") or "").strip() or now_local().isoformat()
    evid_rows: list[dict[str, Any]] = []
    analysed_ts = str(extra.pop("analysed_ts", "") or "").strip()
    for row in list(evidence or []):
        if isinstance(row, dict):
            item = dict(row)
            if not str(item.get("ts") or "").strip():
                item["ts"] = case_ts
            elif not analysed_ts:
                analysed_ts = str(item.get("ts") or "").strip()
            evid_rows.append(item)
        else:
            evid_rows.append(
                {"stream": "log", "ts": case_ts, "line": str(row)[:400]}
            )
    if not analysed_ts:
        analysed_ts = case_ts
    payload: dict[str, Any] = {
        "case_id": case_id,
        "section": section,
        "title": title,
        "signal": signal,
        "expected": expected,
        "observed": observed,
        "sources": src,
        "result": str(result).lower(),
        "evidence": evid_rows,
        "show_evidence": bool(show_evidence),
        "metrics": dict(metrics or {}),
        "ts": case_ts,
        "analysed_ts": analysed_ts,
        "message": message
        or f"{case_id} — {title}: {str(result).upper()}",
    }
    payload.update(extra)
    emit("jama_case", **payload)


def evidence_from_pair(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Notes evidence list from an sbx_verify pair dict."""
    out: list[dict[str, Any]] = []
    for key, label in (("req_evidence", "req"), ("resp_evidence", "resp")):
        line = row.get(key) or ""
        if not line and key == "req_evidence":
            line = row.get("evidence") or ""
        if not line:
            continue
        ts = row.get("req_ts") if label == "req" else row.get("resp_ts")
        out.append(
            {
                "stream": "modbus",
                "ts": ts or "",
                "line": str(line)[:400],
                "label": label,
            }
        )
    if not out and row.get("evidence"):
        out.append(
            {
                "stream": "modbus",
                "ts": row.get("req_ts") or row.get("resp_ts") or "",
                "line": str(row.get("evidence"))[:400],
                "label": "hit",
            }
        )
    return out


def _pair_arrival_summary(row: dict[str, Any]) -> str:
    """Human-readable what arrived vs what was expected for a req/resp pair."""
    req_id = str(row.get("req_id") or "?")
    resp_id = str(row.get("resp_id") or "?")
    same = req_id == resp_id
    has_req = bool(row.get("req_evidence") or (same and row.get("evidence")))
    has_resp = bool(row.get("resp_evidence") or (same and row.get("evidence")))
    if same:
        # Heartbeat-style: one ID both ways.
        if row.get("observed") or has_req:
            return f"{req_id} seen in Modbus log"
        return f"{req_id} not seen in Modbus log"
    parts: list[str] = []
    parts.append(f"{req_id} {'seen' if has_req else 'missing'}")
    parts.append(f"{resp_id} {'seen' if has_resp else 'missing'}")
    return "; ".join(parts)


def emit_sbx_pair_cases(
    emit: EmitFn,
    pairs: list[dict[str, Any]],
    *,
    section: str,
    ids: CaseIdFactory,
    pass_if_missing: Optional[set[str] | frozenset[str]] = None,
) -> None:
    """Emit one JAMA case per SBX req/resp verification pair.

    ``pass_if_missing`` — pair *names* that PASS when not observed (e.g. optional
    MsgFaultCount, or AssignDynamicBusId when commissioning was skipped).
    """
    soft = {str(n) for n in (pass_if_missing or set())}
    for row in pairs:
        observed = bool(row.get("observed"))
        name = str(row.get("name") or "SBX pair")
        req_id = str(row.get("req_id") or "?")
        resp_id = str(row.get("resp_id") or "?")
        soft_ok = (not observed) and name in soft
        if req_id == resp_id:
            expected = f"{name}: message {req_id} present in Modbus log"
        else:
            expected = (
                f"{name}: request {req_id} and response {resp_id} both present"
            )
        if soft_ok:
            if "FaultCount" in name or "fault" in name.lower():
                expected = (
                    f"{name}: optional — absence means no fault traffic (PASS)"
                )
            else:
                expected = (
                    f"{name}: not required for this run (PASS if missing)"
                )
        result = "pass" if (observed or soft_ok) else "fail"
        observed_text = _pair_arrival_summary(row)
        if soft_ok:
            observed_text = f"{observed_text} — treated as PASS (optional / not required)"
        emit_jama_case(
            emit,
            case_id=ids.next("SBX"),
            section=section,
            title=f"SBX comms: {name}",
            signal=name,
            expected=expected,
            observed=observed_text,
            sources="modbus",
            result=result,
            evidence=evidence_from_pair(row),
            show_evidence=True,
            req_id=req_id,
            resp_id=resp_id,
            optional=soft_ok or name in soft,
        )
