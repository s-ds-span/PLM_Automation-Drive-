"""Build a JAMA-style HTML run report from a Drive automation JSONL log."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


REPORT_CSS = """
:root {
  --bg: #f6f8fa;
  --card: #ffffff;
  --ink: #1f2328;
  --muted: #656d76;
  --line: #d0d7de;
  --accent: #0969da;
  --pass: #1a7f37;
  --pass-bg: #dafbe1;
  --fail: #cf222e;
  --fail-bg: #ffebe9;
  --chip: #f6f8fa;
  --section: #24292f;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--ink); margin: 0; padding: 28px 32px 48px;
  line-height: 1.5;
}
h1 { color: var(--section); margin: 0 0 6px; font-size: 1.55rem; font-weight: 700; }
h2 {
  color: var(--section); margin: 32px 0 14px; font-size: 1.05rem; font-weight: 700;
  border-bottom: 2px solid var(--line); padding-bottom: 8px;
}
.meta { color: var(--muted); font-size: 0.9rem; margin-bottom: 18px; }
.badge {
  display: inline-block; padding: 3px 12px; border-radius: 999px;
  font-size: 0.75rem; font-weight: 700; letter-spacing: 0.03em;
}
.badge.pass { background: var(--pass-bg); color: var(--pass); border: 1px solid #4ac26b66; }
.badge.fail { background: var(--fail-bg); color: var(--fail); border: 1px solid #ff818266; }
.summary {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px; margin: 16px 0 22px;
}
.summary .card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 12px; text-align: center; box-shadow: 0 1px 2px #1f23280a;
}
.summary .card .n { font-size: 1.65rem; font-weight: 750; color: var(--ink); }
.summary .card .l { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.04em; margin-top: 2px; }
.summary .card.pass-tone .n { color: var(--pass); }
.summary .card.fail-tone .n { color: var(--fail); }
.case {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; margin: 12px 0; box-shadow: 0 1px 2px #1f23280a;
}
.case.fail { border-color: #ff8182; background: #fff8f8; }
.case-head {
  display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
  justify-content: space-between; margin-bottom: 12px;
}
.case-title { font-weight: 650; color: var(--section); font-size: 0.95rem; }
.metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 8px; margin: 0 0 12px;
}
.metric {
  background: #eef6ff; border: 1px solid #b6d4fe; border-radius: 8px; padding: 8px 10px;
}
.metric label {
  display: block; color: var(--accent); font-size: 0.65rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 2px;
}
.metric span { font-size: 1.05rem; font-weight: 700; color: var(--section); font-variant-numeric: tabular-nums; }
.chips {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 8px; margin: 8px 0;
}
.chip {
  background: var(--chip); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px;
}
.chip label {
  display: block; color: var(--muted); font-size: 0.65rem; font-weight: 650;
  text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 3px;
}
.chip span { color: var(--ink); font-size: 0.86rem; word-break: break-word; }
.notes { margin-top: 12px; }
.notes h4 {
  margin: 0 0 6px; color: var(--muted); font-size: 0.72rem;
  text-transform: uppercase; font-weight: 700; letter-spacing: 0.04em;
}
.evidence {
  background: #f6f8fa; border: 1px solid var(--line); border-radius: 8px;
  padding: 10px 12px; margin: 6px 0; font-size: 0.78rem; color: #424a53;
  white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
.evidence .src { color: var(--accent); font-weight: 650; }
.evidence .ts { color: var(--muted); font-weight: 600; }
.case-ts {
  color: var(--muted); font-size: 0.8rem; margin: 0 0 8px;
  font-variant-numeric: tabular-nums;
}
details.step-list {
  margin-top: 12px; border: 1px solid var(--line); border-radius: 10px;
  background: #fbfcfd; padding: 8px 12px;
}
details.step-list > summary {
  cursor: pointer; font-weight: 650; color: var(--section);
  padding: 6px 0;
}
.step-case {
  border-top: 1px solid var(--line); padding: 10px 0 6px; margin-top: 6px;
}
.step-case:first-of-type { border-top: 0; margin-top: 0; }
.ok { color: var(--pass); } .err { color: var(--fail); } .warn { color: #9a6700; }
a { color: var(--accent); }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; background: var(--card); }
th, td { border-bottom: 1px solid var(--line); padding: 8px 10px; text-align: left; }
th { color: var(--muted); font-size: 0.7rem; text-transform: uppercase; background: #f6f8fa; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; margin: 10px 0; box-shadow: 0 1px 2px #1f23280a;
}
.hero {
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 18px 20px; margin-bottom: 8px; box-shadow: 0 1px 2px #1f23280a;
}
"""


def _load_events(log_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not log_path or not Path(log_path).is_file():
        return events
    for line in Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            events.append(rec)
    return events


def _last(events: list[dict[str, Any]], name: str) -> Optional[dict[str, Any]]:
    for ev in reversed(events):
        if ev.get("event") == name:
            return ev
    return None


def _all(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [ev for ev in events if ev.get("event") == name]


def _phase_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("event") != "pipeline_phase":
            continue
        rows.append({
            "ts": ev.get("ts"),
            "phase": ev.get("phase"),
            "iteration": ev.get("iteration"),
            "message": ev.get("message"),
            "skipped": ev.get("skipped"),
        })
    return rows


_THROTTLE_STEP_SECTIONS = frozenset(
    {
        "Throttle — aggregate steps",
        "Throttle — recover steps",
        "Throttle — current balance",
        "Throttle — current balance (recover)",
        "Throttle — SBX fault (aggregate)",
        "Throttle — SBX fault (recover)",
    }
)


def _normalize_case(ev: dict[str, Any]) -> dict[str, Any]:
    result = str(ev.get("result") or "").lower()
    if result not in ("pass", "fail"):
        result = "pass" if ev.get("observed") in (True, "observed", "pass") else "fail"
    evidence = ev.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [{"stream": "modbus", "ts": "", "line": evidence}]
    show_evidence = ev.get("show_evidence")
    if show_evidence is None:
        # Capture section never shows Notes; empty evidence hidden elsewhere.
        show_evidence = str(ev.get("section") or "") != "Capture"
    return {
        "case_id": ev.get("case_id") or "",
        "section": ev.get("section") or "Other",
        "title": ev.get("title") or ev.get("message") or "",
        "signal": ev.get("signal") or "",
        "expected": ev.get("expected") or "",
        "observed": ev.get("observed") or "",
        "sources": ev.get("sources") or "modbus",
        "result": result,
        "evidence": evidence,
        "show_evidence": bool(show_evidence),
        "metrics": dict(ev.get("metrics") or {}),
        "message": ev.get("message") or "",
        "ts": ev.get("ts") or ev.get("ts_local") or "",
        "analysed_ts": ev.get("analysed_ts") or _first_evidence_ts(evidence) or ev.get("ts") or "",
        "steps": list(ev.get("steps") or []),
        "grouped": bool(ev.get("grouped")),
    }


def _first_evidence_ts(evidence: Any) -> str:
    if not isinstance(evidence, list):
        return ""
    for row in evidence:
        if isinstance(row, dict):
            ts = str(row.get("ts") or "").strip()
            if ts:
                return ts
    return ""


def _is_throttle_step_case(case: dict[str, Any]) -> bool:
    sec = str(case.get("section") or "")
    return sec in _THROTTLE_STEP_SECTIONS or sec.startswith("Throttle —")


def _collapse_throttle_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge per-step THR/REC/BAL JAMA rows into one expandable Throttle case."""
    out: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    inserted = False
    for case in cases:
        if _is_throttle_step_case(case):
            steps.append(case)
            continue
        if steps and not inserted:
            out.append(_build_throttle_group(steps))
            inserted = True
            steps = []
        elif steps:
            # More throttle steps after a non-throttle gap — flush another group.
            out.append(_build_throttle_group(steps))
            steps = []
        out.append(case)
    if steps:
        out.append(_build_throttle_group(steps))
    return out


def _build_throttle_group(steps: list[dict[str, Any]]) -> dict[str, Any]:
    fails = [s for s in steps if str(s.get("result")).lower() == "fail"]
    ok = not fails
    first_ts = next((str(s.get("ts") or "") for s in steps if s.get("ts")), "")
    last_ts = ""
    for s in reversed(steps):
        if s.get("ts"):
            last_ts = str(s.get("ts"))
            break
    agg_n = sum(
        1 for s in steps if "aggregate" in str(s.get("section") or "").lower()
        or str(s.get("case_id") or "").startswith("DRIVE-THR-")
    )
    rec_n = sum(
        1 for s in steps if "recover" in str(s.get("section") or "").lower()
        or str(s.get("case_id") or "").startswith("DRIVE-REC-")
    )
    bal_n = sum(1 for s in steps if str(s.get("case_id") or "").startswith("DRIVE-BAL-"))
    evid: list[dict[str, Any]] = []
    for s in steps:
        for row in s.get("evidence") or []:
            if isinstance(row, dict):
                item = dict(row)
                if not item.get("ts"):
                    item["ts"] = s.get("ts") or ""
                if not item.get("label"):
                    item["label"] = str(s.get("case_id") or s.get("title") or "")[:80]
                evid.append(item)
    return {
        "case_id": "DRIVE-THR-GROUP",
        "section": "Throttle",
        "title": "LB2 throttle / recover sequence (all steps)",
        "signal": "charge_rate_a / LB2 steps / current balance",
        "expected": "Each aggregate/recover/balance step meets its expected checks",
        "observed": (
            f"{len(steps)} step(s): {agg_n} aggregate, {rec_n} recover, "
            f"{bal_n} balance — {len(fails)} failed"
            + (f" · first @{first_ts}" if first_ts else "")
            + (f" · last @{last_ts}" if last_ts and last_ts != first_ts else "")
        ),
        "sources": "modbus, gateway, lb2",
        "result": "pass" if ok else "fail",
        "evidence": evid[:40],
        "show_evidence": True,
        "metrics": {
            "step_count": len(steps),
            "aggregate_steps": agg_n,
            "recover_steps": rec_n,
            "balance_steps": bal_n,
            "failed_steps": len(fails),
            "first_ts": first_ts,
            "last_ts": last_ts,
        },
        "message": "Grouped throttle steps — expand for per-step detail",
        "ts": first_ts or last_ts,
        "steps": steps,
        "grouped": True,
    }


def build_report_context(
    *,
    events: list[dict[str, Any]],
    meta: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    failed = _last(events, "run_failed") or _last(events, "error")
    complete = _last(events, "run_complete") or _last(events, "dashboard_status")
    raw_cases = [_normalize_case(ev) for ev in _all(events, "jama_case")]
    cases = _collapse_throttle_cases(raw_cases)
    passed = sum(1 for c in cases if c["result"] == "pass")
    failed_n = sum(1 for c in cases if c["result"] == "fail")
    total = len(cases)
    pass_rate = round(100.0 * passed / total, 1) if total else 0.0

    pipeline_ok = failed is None and (
        complete is not None
        or any(e.get("event") == "artifact_ready" for e in events)
    )
    if total:
        ok = pipeline_ok and failed_n == 0
    else:
        ok = bool(pipeline_ok)

    capacity = _last(events, "panel_capacity") or {}
    ratings = _last(events, "panel_ratings") or {}
    ev_charging = _last(events, "ev_charging") or {}
    commission = _last(events, "commission_complete") or {}
    commission_report = _last(events, "commission_report") or {}
    throttle_engaged = _last(events, "throttle_engaged")
    throttle_6 = _last(events, "throttle_step_6a")
    throttle_0 = _last(events, "throttle_zero")
    recovery = _last(events, "recovery_verified") or _last(events, "recovery_timeout")
    feeder = _last(events, "feeder_amps") or {}
    boot_ready = _last(events, "ev_ready") or {}
    started = events[0].get("ts") if events else None
    finished = (failed or complete or (events[-1] if events else {})).get("ts")

    sections: list[dict[str, Any]] = []
    section_map: dict[str, list[dict[str, Any]]] = dict()
    for case in cases:
        sec = str(case["section"])
        if sec not in section_map:
            section_map[sec] = []
            sections.append({"name": sec, "cases": section_map[sec]})
        section_map[sec].append(case)

    capture_streams = []
    for ev in _all(events, "capture_stream_status"):
        capture_streams.append({
            "stream": ev.get("stream"),
            "started_at": ev.get("started_at"),
            "ok": ev.get("ok"),
            "data_available": ev.get("data_available"),
            "error": ev.get("error"),
            "message": ev.get("message"),
        })

    sbx_pairs = [
        {
            "name": ev.get("name"),
            "observed": ev.get("observed"),
            "evidence": ev.get("evidence"),
            "req_id": ev.get("req_id"),
            "resp_id": ev.get("resp_id"),
            "message": ev.get("message"),
        }
        for ev in _all(events, "sbx_msg_verify")
    ]
    handshake_pairs = [
        {
            "name": ev.get("name"),
            "observed": ev.get("observed"),
            "evidence": ev.get("evidence"),
            "req_id": ev.get("req_id"),
            "resp_id": ev.get("resp_id"),
            "message": ev.get("message"),
        }
        for ev in _all(events, "handshake_verify")
    ]

    sbx_abnormalities = [
        {
            "ts": ev.get("ts"),
            "kind": ev.get("kind"),
            "message": ev.get("message"),
            "pipeline_phase": ev.get("pipeline_phase"),
            "phase": ev.get("phase"),
            "fault_mask": ev.get("fault_mask"),
            "charging_state": ev.get("charging_state"),
            "evidence": (ev.get("evidence") or "")[:400],
        }
        for ev in _all(events, "sbx_abnormality")
    ]
    sbx_summary = _last(events, "sbx_abnormality_summary") or {}
    # Summary payload is authoritative when individual live events were missed
    # (background-thread emit race) but in-memory hits were still counted.
    summary_hits = sbx_summary.get("abnormalities") or []
    if isinstance(summary_hits, list) and summary_hits:
        seen = {
            f"{r.get('kind')}|{r.get('ts')}|{r.get('message')}"
            for r in sbx_abnormalities
        }
        for hit in summary_hits:
            if not isinstance(hit, dict):
                continue
            key = f"{hit.get('kind')}|{hit.get('ts')}|{hit.get('message')}"
            if key in seen:
                continue
            seen.add(key)
            sbx_abnormalities.append(
                {
                    "ts": hit.get("ts"),
                    "kind": hit.get("kind"),
                    "message": hit.get("message"),
                    "pipeline_phase": hit.get("pipeline_phase"),
                    "phase": hit.get("phase"),
                    "fault_mask": hit.get("fault_mask"),
                    "charging_state": hit.get("charging_state"),
                    "evidence": (hit.get("evidence") or "")[:400],
                }
            )
    if not sbx_abnormalities:
        cap_dir = meta.get("capture_session_dir")
        if cap_dir:
            try:
                from drive_automation.sbx_verify import (
                    resolve_modbus_log,
                    scan_modbus_log_abnormalities,
                )

                modbus_path = resolve_modbus_log(Path(str(cap_dir)))
                if modbus_path is not None:
                    sbx_abnormalities = scan_modbus_log_abnormalities(modbus_path)
            except Exception:
                pass
    summary_count = sbx_summary.get("count")
    try:
        summary_count_n = int(summary_count) if summary_count is not None else 0
    except (TypeError, ValueError):
        summary_count_n = 0
    sbx_count = max(len(sbx_abnormalities), summary_count_n)

    stop_reason = None
    if failed:
        stop_reason = failed.get("message") or failed.get("error")
    elif failed_n and total:
        first_fail = next((c for c in cases if c["result"] == "fail"), None)
        if first_fail:
            stop_reason = (
                f"Mismatch on {first_fail.get('case_id')}: "
                f"expected {first_fail.get('expected')}, "
                f"got {first_fail.get('observed')}"
            )

    return {
        "run_id": run_id,
        "ok": bool(ok),
        "started_at": started,
        "finished_at": finished,
        "meta": meta,
        "failure": failed,
        "stop_reason": stop_reason,
        "stats": {
            "tests": total,
            "passed": passed,
            "failed": failed_n,
            "pass_rate": pass_rate,
        },
        "sections": sections,
        "cases": cases,
        "capture": {
            "streams": capture_streams,
            "summary": _last(events, "capture_status_summary"),
        },
        "commission": {
            "skipped": bool(commission_report.get("skipped")),
            "ok": commission_report.get("ok", commission.get("ok")),
            "message": commission_report.get("message") or commission.get("message"),
            "breaker_instance_id": commission.get("breaker_instance_id"),
            "breaker_rating_a": (
                commission_report.get("breaker_rating_a")
                or (
                    (commission.get("breaker_rating_ma") or 0) / 1000.0
                    if commission.get("breaker_rating_ma")
                    else None
                )
            ),
            "top_position": (
                commission_report.get("top_position")
                or commission.get("top_position")
            ),
            "bottom_position": (
                commission_report.get("bottom_position")
                or commission.get("bottom_position")
            ),
            "services_restarted": commission.get("services_restarted")
            or commission_report.get("services_restarted"),
            "discovery_state": commission.get("discovery_state")
            or commission_report.get("discovery_state"),
            "discovery_successful": commission.get("discovery_successful")
            or commission_report.get("discovery_successful"),
        },
        "post_commission_sbx": {"pairs": sbx_pairs},
        "boot": {
            "message": boot_ready.get("message"),
            "startup_session_ready": boot_ready.get("startup_session_ready"),
            "availability": _last(events, "availability"),
        },
        "handshake": {"pairs": handshake_pairs},
        "sbx_monitor": {
            "count": sbx_count,
            "summary_message": sbx_summary.get("message"),
            "modbus_log": sbx_summary.get("modbus_log"),
            "abnormalities": sbx_abnormalities,
        },
        "panel_ratings": {
            "site_utility_rating_a": ratings.get("site_utility_rating_a")
            or capacity.get("site_utility_rating_a"),
            "conductor_rating_a": ratings.get("conductor_rating_a"),
            "busbar_rating_a": ratings.get("busbar_rating_a")
            or capacity.get("busbar_rating_a"),
            "feeder_breaker_rating_a": ratings.get("feeder_breaker_rating_a"),
            "limit_a": ratings.get("limit_a"),
            "throttle_threshold_a": ratings.get("throttle_threshold_a"),
            "feeder_combined_a": feeder.get("combined_a"),
            "feeder_line_a_a": feeder.get("line_a_a"),
            "feeder_line_b_a": feeder.get("line_b_a"),
            "message": ratings.get("message"),
        },
        "panel_capacity": {
            "site_utility_rating_a": capacity.get("site_utility_rating_a"),
            "busbar_rating_a": capacity.get("busbar_rating_a"),
            "commissioning_status": capacity.get("commissioning_status"),
        },
        "charge": {
            "ev_draw_a": ev_charging.get("ev_draw_a") or ev_charging.get("ev_max_amps"),
            "commissioned_drive_a": ev_charging.get("commissioned_drive_a"),
            "ev_draw_fraction": ev_charging.get("ev_draw_fraction"),
            "duty_cycle_pct": ev_charging.get("duty_cycle_pct"),
            "adv_current_a": ev_charging.get("adv_current_a"),
            "site_capacity_a": ev_charging.get("site_capacity_a"),
        },
        "throttle": {
            "engaged": bool(throttle_engaged),
            "engaged_message": (throttle_engaged or {}).get("message"),
            "engaged_charge_rate_a": (throttle_engaged or {}).get("charge_rate_a"),
            "commanded_amps": (throttle_engaged or {}).get("commanded_amps"),
            "meter_amps": (throttle_engaged or {}).get("meter_amps"),
            "duty_cycle_pct": (throttle_engaged or {}).get("duty_cycle_pct"),
            "step_6a": bool(throttle_6),
            "step_6a_charge_rate_a": (throttle_6 or {}).get("charge_rate_a"),
            "zero": bool(throttle_0),
            "zero_charge_rate_a": (throttle_0 or {}).get("charge_rate_a"),
            "recovery_event": (recovery or {}).get("event"),
            "recovery_charge_rate_a": (recovery or {}).get("charge_rate_a"),
            "load_increased": bool(_last(events, "panel_load_increased")),
        },
        "timeline": _phase_timeline(events),
        "commands": [
            {
                "ts": ev.get("ts") or ev.get("ts_local") or "",
                "command": ev.get("command") or "",
                "exit": ev.get("exit"),
                "output": ev.get("output") or "",
                "message": ev.get("message") or "",
            }
            for ev in _all(events, "gateway_cmd")
        ],
        "event_count": len(events),
    }


def _fmt_metric(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):g} A"
    except (TypeError, ValueError):
        return str(v)


def _render_case(case: dict[str, Any], esc) -> str:
    result = case.get("result") or "fail"
    badge = (
        '<span class="badge pass">PASS</span>'
        if result == "pass"
        else '<span class="badge fail">FAIL</span>'
    )
    cls = "case fail" if result == "fail" else "case"

    metrics = case.get("metrics") or {}
    metrics_html = ""
    metric_order = (
        ("sbx_request_a", "SBX request (panel→Drive)"),
        ("observed_charge_rate_a", "Observed charge rate"),
        ("panel_total_a", "Panel total (gateway)"),
        ("lb2_load_a", "LB2 load (commanded)"),
        ("lb2_meter_a", "LB2 meter"),
        ("lb2_achieved_a", "LB2 achieved"),
        ("lb2_plus_drive_a", "LB2 + drive (expected)"),
        ("fault_mask", "SBX fault_mask"),
        ("breaker_rating_a", "Breaker rating"),
        ("top_position", "Top tab"),
        ("bottom_position", "Bottom tab"),
        ("discovery_state", "Discovery"),
    )
    # Internal balance bookkeeping — keep in JSON events, hide from HTML/PDF.
    hidden_metrics = {
        "lb2_balance_a",
        "lb2_balance_source",
        "balance_error_a",
        "balance_ok",
    }
    metric_blocks = ""
    for key, label in metric_order:
        if key not in metrics or key in hidden_metrics:
            continue
        val = metrics.get(key)
        if key.endswith("_a") or key in ("breaker_rating_a", "balance_error_a"):
            shown = _fmt_metric(val)
        elif key == "balance_ok":
            shown = "yes" if val else "no"
        else:
            shown = "—" if val is None or val == "" else str(val)
        metric_blocks += (
            f'<div class="metric"><label>{esc(label)}</label>'
            f"<span>{esc(shown)}</span></div>"
        )
    # Any other metrics
    known = {k for k, _ in metric_order} | hidden_metrics
    for key, val in metrics.items():
        if key in known:
            continue
        metric_blocks += (
            f'<div class="metric"><label>{esc(key)}</label>'
            f"<span>{esc(val)}</span></div>"
        )
    if metric_blocks:
        metrics_html = f'<div class="metrics">{metric_blocks}</div>'

    evidence_html = ""
    evid = case.get("evidence") or []
    show_evidence = bool(case.get("show_evidence", True))
    if show_evidence and evid:
        blocks = ""
        for row in evid:
            stream = esc(row.get("stream") or "modbus")
            ts = esc(row.get("ts") or "")
            label = esc(row.get("label") or "")
            line = esc(row.get("line") or "")
            head = f'<span class="src">{stream}</span>'
            if ts:
                head += f' · <span class="ts">{ts}</span>'
            if label:
                head += f" ({label})"
            blocks += f'<div class="evidence">{head}\n{line}</div>'
        evidence_html = (
            f'<div class="notes"><h4>Notes — log evidence ({len(evid)})</h4>'
            f"{blocks}</div>"
        )
    elif show_evidence and not evid:
        # Soft SBX misses still show an empty Notes cue.
        if str(case.get("section") or "").startswith("SBX"):
            evidence_html = (
                '<div class="notes"><h4>Notes — log evidence</h4>'
                '<div class="evidence">No matching Modbus line in the watch window.'
                "</div></div>"
            )

    case_ts = str(case.get("ts") or "").strip()
    analysed_ts = str(case.get("analysed_ts") or "").strip()
    ts_bits = []
    if case_ts:
        ts_bits.append(f"Recorded: <strong>{esc(case_ts)}</strong>")
    if analysed_ts and analysed_ts != case_ts:
        ts_bits.append(f"Log analysed: <strong>{esc(analysed_ts)}</strong>")
    elif analysed_ts:
        ts_bits.append(f"Log analysed: <strong>{esc(analysed_ts)}</strong>")
    ts_html = f'<div class="case-ts">{" · ".join(ts_bits)}</div>' if ts_bits else ""

    steps_html = ""
    steps = case.get("steps") or []
    if case.get("grouped") and steps:
        inner = ""
        for step in steps:
            step_badge = (
                '<span class="badge pass">PASS</span>'
                if str(step.get("result")).lower() == "pass"
                else '<span class="badge fail">FAIL</span>'
            )
            step_ts = esc(step.get("ts") or "—")
            step_evid = ""
            for row in step.get("evidence") or []:
                if not isinstance(row, dict):
                    continue
                st = esc(row.get("ts") or step.get("ts") or "")
                ts_bit = f' · <span class="ts">{st}</span>' if st else ""
                step_evid += (
                    f'<div class="evidence">'
                    f'<span class="src">{esc(row.get("stream") or "log")}</span>'
                    f"{ts_bit}"
                    f'\n{esc(row.get("line") or "")}</div>'
                )
            inner += f"""
      <div class="step-case">
        <div class="case-head">
          <div class="case-title">{esc(step.get('case_id'))} — {esc(step.get('title'))}</div>
          {step_badge}
        </div>
        <div class="case-ts">Step timestamp: <strong>{step_ts}</strong></div>
        <div class="chips">
          <div class="chip"><label>Section</label><span>{esc(step.get('section'))}</span></div>
          <div class="chip"><label>Expected</label><span>{esc(step.get('expected'))}</span></div>
          <div class="chip"><label>Observed</label><span>{esc(step.get('observed'))}</span></div>
        </div>
        {step_evid}
      </div>
            """
        steps_html = (
            f'<details class="step-list">'
            f"<summary>Show {len(steps)} throttle step detail(s)</summary>"
            f"{inner}</details>"
        )

    return f"""
    <div class="{cls}">
      <div class="case-head">
        <div class="case-title">{esc(case.get('case_id'))} — {esc(case.get('title'))}</div>
        {badge}
      </div>
      {ts_html}
      {metrics_html}
      <div class="chips">
        <div class="chip"><label>Signal</label><span>{esc(case.get('signal'))}</span></div>
        <div class="chip"><label>Expected</label><span>{esc(case.get('expected'))}</span></div>
        <div class="chip"><label>Observed</label><span>{esc(case.get('observed'))}</span></div>
        <div class="chip"><label>Sources</label><span>{esc(case.get('sources'))}</span></div>
      </div>
      {evidence_html}
      {steps_html}
    </div>
    """


def render_drive_report_html(
    *,
    context: dict[str, Any],
    download_url: str = "",
) -> str:
    ok = bool(context.get("ok"))
    badge = (
        '<span class="badge pass">PASS</span>'
        if ok
        else '<span class="badge fail">FAIL</span>'
    )
    meta = context.get("meta") or {}
    stats = context.get("stats") or {}
    fail = context.get("failure") or {}

    def esc(v: Any) -> str:
        if v is None or v == "":
            return "—"
        return html.escape(str(v))

    sections_html = ""
    for sec in context.get("sections") or []:
        name = esc(sec.get("name"))
        sections_html += f"<h2>{name}</h2>\n"
        for case in sec.get("cases") or []:
            sections_html += _render_case(case, esc)

    if not sections_html:
        sections_html = (
            '<div class="card warn">No JAMA cases recorded in this run. '
            "Re-run with v1.12+ to emit SBX / throttle evidence cases.</div>"
        )

    timeline_rows = ""
    for row in context.get("timeline") or []:
        timeline_rows += (
            "<tr>"
            f"<td>{esc((row.get('ts') or '')[:19])}</td>"
            f"<td>{esc(row.get('phase'))}</td>"
            f"<td>{esc(row.get('iteration'))}</td>"
            f"<td>{esc(row.get('message'))}</td>"
            "</tr>"
        )
    if not timeline_rows:
        timeline_rows = '<tr><td colspan="4">No pipeline phases recorded.</td></tr>'

    command_rows = ""
    for row in context.get("commands") or []:
        command_rows += (
            "<tr>"
            f"<td>{esc((row.get('ts') or '')[:23])}</td>"
            f"<td><code>{esc(row.get('command'))}</code></td>"
            f"<td>{esc(row.get('exit'))}</td>"
            f"<td>{esc((row.get('output') or '')[:180])}</td>"
            "</tr>"
        )
    if command_rows:
        commands_html = f"""
<h2>Gateway commands ({len(context.get('commands') or [])})</h2>
<div class="card">
<table>
  <thead><tr><th>Time</th><th>Command</th><th>Exit</th><th>Output</th></tr></thead>
  <tbody>{command_rows}</tbody>
</table>
</div>
"""
    else:
        commands_html = ""

    stop = context.get("stop_reason") or (
        fail.get("message") or fail.get("error") if fail else None
    )
    stop_html = (
        f'<p class="err"><strong>Stopped:</strong> {esc(stop)}</p>' if stop else ""
    )

    dl_bits = []
    if download_url:
        dl_bits.append(f'<a href="{html.escape(download_url)}">Download full zip</a>')
    modbus_url = str(context.get("modbus_url") or "")
    pilot_url = str(context.get("pilot_url") or "")
    ev_loads_url = str(context.get("ev_loads_url") or "")
    lb2_url = str(context.get("lb2_url") or "")
    panel_url = str(context.get("panel_url") or "")
    meta = context.get("meta") or {}
    if not modbus_url:
        modbus_url = str(meta.get("modbus_url") or "")
    if not pilot_url:
        pilot_url = str(meta.get("pilot_url") or "")
    if not ev_loads_url:
        ev_loads_url = str(meta.get("ev_loads_url") or "")
    if not lb2_url:
        lb2_url = str(meta.get("lb2_url") or "")
    if not panel_url:
        panel_url = str(meta.get("panel_url") or "")
    if modbus_url:
        dl_bits.append(
            f'<a href="{html.escape(modbus_url)}">Download Modbus / SBX log</a>'
        )
    if pilot_url:
        dl_bits.append(f'<a href="{html.escape(pilot_url)}">Download Pilot log</a>')
    if ev_loads_url:
        dl_bits.append(f'<a href="{html.escape(ev_loads_url)}">Download EV loads log</a>')
    if lb2_url:
        dl_bits.append(f'<a href="{html.escape(lb2_url)}">Download LB2 log</a>')
    if panel_url:
        dl_bits.append(f'<a href="{html.escape(panel_url)}">Download Panel current log</a>')
    dl = f"<p>{' · '.join(dl_bits)}</p>" if dl_bits else ""

    sbx = context.get("sbx_monitor") or {}
    sbx_list = sbx.get("abnormalities") or []
    try:
        sbx_count = int(sbx.get("count") or len(sbx_list) or 0)
    except (TypeError, ValueError):
        sbx_count = len(sbx_list)
    summary_msg = str(sbx.get("summary_message") or "")
    if sbx_count > 0 or sbx_list:
        sbx_rows = ""
        for ab in sbx_list:
            phase = ab.get("pipeline_phase") or ab.get("phase") or "—"
            detail = ab.get("message") or ""
            if ab.get("fault_mask"):
                detail = f"{detail} (fault_mask={ab.get('fault_mask')})"
            sbx_rows += (
                "<tr>"
                f"<td>{esc((ab.get('ts') or '')[:19])}</td>"
                f"<td>{esc(ab.get('kind'))}</td>"
                f"<td>{esc(phase)}</td>"
                f"<td>{esc(detail)}</td>"
                "</tr>"
            )
        if not sbx_rows:
            sbx_rows = (
                "<tr><td colspan='4'>"
                f"{esc(summary_msg or f'{sbx_count} abnormality(ies) recorded')}"
                "</td></tr>"
            )
        sbx_html = f"""
<h2>SBX abnormalities ({esc(sbx_count)})</h2>
<p class="warn">Faults, undecoded bytes, or charge-point resets observed in the Modbus log during the run.</p>
<div class="card">
<table>
  <thead><tr><th>Time</th><th>Kind</th><th>Phase</th><th>Detail</th></tr></thead>
  <tbody>{sbx_rows}</tbody>
</table>
</div>
"""
    else:
        sbx_html = (
            '<h2>SBX monitor</h2>'
            '<p class="ok">No SBX abnormalities detected in the Modbus log.</p>'
        )

    tests = stats.get("tests") or 0
    passed = stats.get("passed") or 0
    failed_n = stats.get("failed") or 0
    rate = stats.get("pass_rate") or 0

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Drive JAMA Test Report — {esc(context.get('run_id'))}</title>
<style>{REPORT_CSS}</style>
</head><body>
<div class="hero">
<h1>Drive · JAMA Test Report</h1>
<div class="meta">
  Run ID: {esc(context.get('run_id'))}<br>
  Charger: {esc(meta.get('charger_label'))} · Operator: {esc(meta.get('operator'))}<br>
  {esc(context.get('started_at'))} — {esc(context.get('finished_at'))}<br>
  Generated: {esc(datetime.now(timezone.utc).isoformat())}
</div>
</div>

<div class="summary">
  <div class="card"><div class="n">{esc(tests)}</div><div class="l">Tests</div></div>
  <div class="card pass-tone"><div class="n">{esc(passed)}</div><div class="l">Passed</div></div>
  <div class="card fail-tone"><div class="n">{esc(failed_n)}</div><div class="l">Failed</div></div>
  <div class="card"><div class="n">{esc(rate)}%</div><div class="l">Pass rate</div></div>
  <div class="card"><div class="n">{badge}</div><div class="l">Overall</div></div>
</div>
{stop_html}
{dl}

{sbx_html}

<div class="card">
  <div class="chips">
    <div class="chip"><label>Test name</label><span>{esc(meta.get('test_name'))}</span></div>
    <div class="chip"><label>Breaker rating</label><span>{esc(meta.get('breaker_rating_a'))} A</span></div>
    <div class="chip"><label>Use LB2</label><span>{esc(meta.get('use_lb2'))}</span></div>
    <div class="chip"><label>EV URL</label><span>{esc(meta.get('ev_url'))}</span></div>
  </div>
</div>

{sections_html}

{commands_html}

<h2>Phase timeline</h2>
<div class="card">
<table>
  <thead><tr><th>Time</th><th>Phase</th><th>Iter</th><th>Message</th></tr></thead>
  <tbody>{timeline_rows}</tbody>
</table>
</div>
<p class="meta">Events in log: {esc(context.get('event_count'))}</p>
</body></html>
"""


def write_drive_report(
    *,
    artifacts_root: Path,
    run_id: str,
    log_path: Optional[Path],
    meta: dict[str, Any],
    download_url: str = "",
    modbus_url: str = "",
    pilot_url: str = "",
    ev_loads_url: str = "",
    lb2_url: str = "",
    panel_url: str = "",
) -> dict[str, Any]:
    """Write drive_report.html + report_context.json under artifacts/<run_id>/."""
    from drive_automation.artifacts import resolve_artifact_dir

    run_dir = resolve_artifact_dir(artifacts_root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    events = _load_events(Path(log_path)) if log_path else []
    context = build_report_context(events=events, meta=meta, run_id=run_id)
    context["modbus_url"] = modbus_url or ""
    context["pilot_url"] = pilot_url or ""
    context["ev_loads_url"] = ev_loads_url or ""
    context["lb2_url"] = lb2_url or ""
    context["panel_url"] = panel_url or ""
    html_body = render_drive_report_html(
        context=context,
        download_url=download_url or f"/api/artifacts/{run_id}/download",
    )
    report_path = run_dir / "drive_report.html"
    context_path = run_dir / "report_context.json"
    report_path.write_text(html_body, encoding="utf-8")
    context_path.write_text(
        json.dumps(context, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return {
        "report_html": str(report_path),
        "report_context": str(context_path),
        "ok": context.get("ok"),
        "stats": context.get("stats"),
        "report_url": f"/api/artifacts/{run_id}/report",
    }
