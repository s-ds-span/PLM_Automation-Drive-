#!/usr/bin/env python3
"""Lab Automation Pro Track — daily coach that pushes you to the finish line.

Usage:
  python scripts/learning_coach.py              # today's session + status
  python scripts/learning_coach.py today
  python scripts/learning_coach.py week
  python scripts/learning_coach.py status
  python scripts/learning_coach.py done ["optional note"]
  python scripts/learning_coach.py skip [reason] # marks missed (adds debt)
  python scripts/learning_coach.py gate list|pass <gate-id>
  python scripts/learning_coach.py review       # Sunday / weekly retrospective
  python scripts/learning_coach.py streak
  python scripts/learning_coach.py calendar     # upcoming 7 days
  python scripts/learning_coach.py graduate     # final rubric

Designed for: 1h Mon–Fri, 4h Sat, 4h Sun (13 h/week).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
PATH_DIR = ROOT / "docs" / "learning_path"
CURRICULUM_PATH = PATH_DIR / "curriculum.json"
PROGRESS_PATH = PATH_DIR / "progress.json"
NOTES_DIR = PATH_DIR / "notes"

MAX_DEBT = 3


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"Missing {path}. Run: python docs/learning_path/build_curriculum.py")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_progress(progress: dict[str, Any]) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps(progress, indent=2) + "\n", encoding="utf-8")


def _default_progress(curriculum: dict[str, Any]) -> dict[str, Any]:
    return {
        "program_name": curriculum["program"]["name"],
        "started_on": None,
        "sessions": dict(),  # id -> {status, note, completed_at}
        "gates": dict(),  # id -> {passed_at, note}
        "reviews": list(),
        "graduation": None,
    }


def _load_progress(curriculum: dict[str, Any]) -> dict[str, Any]:
    if PROGRESS_PATH.is_file():
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    progress = _default_progress(curriculum)
    _save_progress(progress)
    return progress


def _all_sessions(curriculum: dict[str, Any]) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for week in curriculum["weeks"]:
        for session in week["sessions"]:
            row = dict(session)
            row["week"] = week["week"]
            row["week_title"] = week["title"]
            row["phase"] = week["phase"]
            row["week_deadline"] = week["deadline"]
            sessions.append(row)
    return sessions


def _session_by_date(curriculum: dict[str, Any], day: date) -> Optional[dict[str, Any]]:
    key = day.isoformat()
    for session in _all_sessions(curriculum):
        if session["date"] == key:
            return session
    return None


def _week_for(curriculum: dict[str, Any], day: date) -> Optional[dict[str, Any]]:
    for week in curriculum["weeks"]:
        if week["start"] <= day.isoformat() <= week["end"]:
            return week
    return None


def _status_of(progress: dict[str, Any], session_id: str) -> str:
    entry = progress.get("sessions", {}).get(session_id) or {}
    return str(entry.get("status") or "pending")


def _debt_sessions(curriculum: dict[str, Any], progress: dict[str, Any], today: date) -> list[dict[str, Any]]:
    debt: list[dict[str, Any]] = []
    for session in _all_sessions(curriculum):
        session_day = date.fromisoformat(session["date"])
        if session_day >= today:
            continue
        st = _status_of(progress, session["id"])
        if st not in {"done", "skipped_excused"}:
            debt.append(session)
    return debt


def _on_track_line(curriculum: dict[str, Any], progress: dict[str, Any], today: date) -> tuple[str, str]:
    """Return (emoji_label, detail)."""
    start = date.fromisoformat(curriculum["program"]["start_date"])
    end = date.fromisoformat(curriculum["program"]["end_date"])
    if today < start:
        days = (start - today).days
        return ("WAIT", f"Program starts {start.isoformat()} ({days} day(s) — use time to gather books/bookmarks)")
    if today > end:
        grad = progress.get("graduation")
        if grad:
            return ("DONE", "Program complete. Keep the 1h/week maintenance habit.")
        return ("LATE", "Past end date — finish debt + graduation rubric now.")

    debt = _debt_sessions(curriculum, progress, today)
    if len(debt) > MAX_DEBT:
        return (
            "CRITICAL",
            f"Debt={len(debt)} (max {MAX_DEBT}). Saturday is burn-down only — no new rabbit holes.",
        )
    if debt:
        ids = ", ".join(s["id"] for s in debt[:5])
        more = "" if len(debt) <= 5 else f" (+{len(debt) - 5} more)"
        return ("BEHIND", f"Debt={len(debt)}: {ids}{more}. Catch up this weekend.")

    # Expected progress: sessions with date < today should be done
    done = sum(
        1
        for s in _all_sessions(curriculum)
        if date.fromisoformat(s["date"]) < today and _status_of(progress, s["id"]) == "done"
    )
    due = sum(1 for s in _all_sessions(curriculum) if date.fromisoformat(s["date"]) < today)
    if due and done == due:
        return ("ON_TRACK", f"All {due} past sessions complete. Keep the streak.")
    return ("ON_TRACK", "No debt. Execute today's block.")


def _print_header(curriculum: dict[str, Any], progress: dict[str, Any], today: date) -> None:
    prog = curriculum["program"]
    label, detail = _on_track_line(curriculum, progress, today)
    print("=" * 64)
    print(f"{prog['name']}")
    print(f"{prog['tagline']}")
    print(f"Today: {today.isoformat()} ({today.strftime('%A')})")
    print(f"Window: {prog['start_date']} → {prog['end_date']}  |  {prog['hours_per_week']} h/week")
    print(f"Status: [{label}] {detail}")
    print("=" * 64)


def _print_session(session: dict[str, Any], progress: dict[str, Any]) -> None:
    st = _status_of(progress, session["id"])
    print(f"\nSession {session['id']}  [{st}]  ·  {session['hours']}h  ·  Week {session['week']}")
    print(f"Phase: {session['phase']}")
    print(f"Title: {session['title']}")
    print("Tasks:")
    for task in session["tasks"]:
        print(f"  - [ ] {task}")
    print(f"Done when: {session['done_when']}")
    print(f"Week deadline: {session['week_deadline']} (Sun)")


def cmd_today(curriculum: dict[str, Any], progress: dict[str, Any], today: date) -> int:
    _print_header(curriculum, progress, today)
    start = date.fromisoformat(curriculum["program"]["start_date"])
    if today < start:
        print("\nPre-start checklist:")
        print("  - [ ] Buy/borrow: Effective Python, Release It!")
        print("  - [ ] Bookmark Missing Semester + HiveMQ MQTT Essentials")
        print("  - [ ] Create calendar blocks: Mon–Fri 1h, Sat 4h, Sun 4h")
        print("  - [ ] Run this coach every study day before you start")
        print(f"\nFirst session: {start.isoformat()} (Monday)")
        first = _session_by_date(curriculum, start)
        if first:
            _print_session(first, progress)
        return 0

    session = _session_by_date(curriculum, today)
    if not session:
        print("\nNo session scheduled today (outside program window).")
        debt = _debt_sessions(curriculum, progress, today)
        if debt:
            print(f"You still have {len(debt)} debt session(s). Burn them down:")
            for s in debt[:7]:
                print(f"  - {s['id']} {s['date']} {s['title']}")
        return 0

    week = _week_for(curriculum, today)
    if week:
        print(f"\nWeek {week['week']} goal: {week['goal']}")
        print("Resources:")
        for url in week["resources"]:
            print(f"  · {url}")

    _print_session(session, progress)

    debt = _debt_sessions(curriculum, progress, today)
    if debt:
        print(f"\n⚠ Debt queue ({len(debt)}) — schedule before new deep-dives:")
        for s in debt:
            print(f"  · {s['id']} | {s['date']} | {s['title']}")

    if today.weekday() == 6:  # Sunday
        print("\nSunday rule: last 30 minutes → `python scripts/learning_coach.py review`")

    label, _ = _on_track_line(curriculum, progress, today)
    if label == "CRITICAL":
        print("\nPUSH: Do NOT start new optional reading. Close debt sessions first.")
        print("      Mark each with: python scripts/learning_coach.py done \"caught up\"")
    elif label == "BEHIND":
        print("\nPUSH: After today's block, schedule debt on Saturday morning first.")
    else:
        print("\nPUSH: Start a timer for the block length. Phone in another room.")
        print("      When finished: python scripts/learning_coach.py done")

    return 0


def cmd_week(curriculum: dict[str, Any], progress: dict[str, Any], today: date) -> int:
    _print_header(curriculum, progress, today)
    week = _week_for(curriculum, today)
    if not week:
        # show nearest upcoming week
        for w in curriculum["weeks"]:
            if w["start"] >= today.isoformat():
                week = w
                break
        if not week:
            week = curriculum["weeks"][-1]
    assert week is not None
    print(f"\nWeek {week['week']}: {week['title']}")
    print(f"Phase: {week['phase']}")
    print(f"Deadline: {week['deadline']}  ·  Planned hours: {week['hours_planned']}")
    print(f"Goal: {week['goal']}\n")
    for session in week["sessions"]:
        st = _status_of(progress, session["id"])
        mark = {"done": "✓", "skipped": "✗", "skipped_excused": "~"}.get(st, " ")
        print(
            f"  [{mark}] {session['date']} {session['dow']:3} "
            f"{session['hours']}h  {session['id']:10}  {session['title']}"
        )
    return 0


def cmd_status(curriculum: dict[str, Any], progress: dict[str, Any], today: date) -> int:
    _print_header(curriculum, progress, today)
    sessions = _all_sessions(curriculum)
    done = sum(1 for s in sessions if _status_of(progress, s["id"]) == "done")
    total = len(sessions)
    pct = (100.0 * done / total) if total else 0.0
    debt = _debt_sessions(curriculum, progress, today)

    start = date.fromisoformat(curriculum["program"]["start_date"])
    end = date.fromisoformat(curriculum["program"]["end_date"])
    if today < start:
        elapsed_pct = 0.0
    elif today > end:
        elapsed_pct = 100.0
    else:
        elapsed_pct = 100.0 * (today - start).days / max((end - start).days, 1)

    print(f"\nProgress: {done}/{total} sessions ({pct:.1f}%)")
    print(f"Calendar elapsed: {elapsed_pct:.1f}%")
    if pct + 5 < elapsed_pct:
        print("PUSH: Completion % is behind calendar % — protect weekend catch-up.")
    elif pct > elapsed_pct + 5:
        print("Nice: slightly ahead of calendar. Don't burn out; keep quality high.")

    print(f"Debt sessions: {len(debt)} (max allowed before CRITICAL: {MAX_DEBT})")

    print("\nPhase gates:")
    for gate in curriculum["phase_gates"]:
        g = progress.get("gates", {}).get(gate["id"])
        state = "PASS" if g else "OPEN"
        due = gate["deadline"]
        overdue = (not g) and today.isoformat() > due and today >= start
        flag = " ⚠ OVERDUE" if overdue else ""
        print(f"  [{state}] {gate['id']:10} {gate['name']:16} due {due}{flag}")
    return 0


def cmd_done(
    curriculum: dict[str, Any],
    progress: dict[str, Any],
    today: date,
    note: str,
    session_id: str,
) -> int:
    sessions = {s["id"]: s for s in _all_sessions(curriculum)}
    if session_id:
        if session_id not in sessions:
            raise SystemExit(f"Unknown session id: {session_id}")
        sid = session_id
    else:
        session = _session_by_date(curriculum, today)
        if not session:
            raise SystemExit("No session for today. Pass --session w01-mon")
        sid = session["id"]

    if not progress.get("started_on"):
        progress["started_on"] = today.isoformat()

    progress.setdefault("sessions", {})[sid] = {
        "status": "done",
        "note": note,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_progress(progress)
    print(f"✓ Marked {sid} done." + (f" Note: {note}" if note else ""))
    debt = _debt_sessions(curriculum, progress, today)
    if debt:
        print(f"Remaining debt: {len(debt)}. Next: {debt[0]['id']} — {debt[0]['title']}")
    else:
        print("No debt. See you at the next block.")
    return 0


def cmd_skip(
    curriculum: dict[str, Any],
    progress: dict[str, Any],
    today: date,
    reason: str,
    excused: bool,
    session_id: str,
) -> int:
    sessions = {s["id"]: s for s in _all_sessions(curriculum)}
    if session_id:
        sid = session_id
        if sid not in sessions:
            raise SystemExit(f"Unknown session id: {sid}")
    else:
        session = _session_by_date(curriculum, today)
        if not session:
            raise SystemExit("No session for today. Pass --session …")
        sid = session["id"]

    status = "skipped_excused" if excused else "skipped"
    progress.setdefault("sessions", {})[sid] = {
        "status": status,
        "note": reason or "",
        "completed_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_progress(progress)
    print(f"Marked {sid} as {status}.")
    if not excused:
        print("This counts as DEBT until you redo it: learning_coach.py done --session " + sid)
        print("PUSH: Put it first on Saturday.")
    return 0


def cmd_gate(
    curriculum: dict[str, Any],
    progress: dict[str, Any],
    action: str,
    gate_id: str,
    note: str,
) -> int:
    gates = {g["id"]: g for g in curriculum["phase_gates"]}
    if action == "list":
        for gate in curriculum["phase_gates"]:
            g = progress.get("gates", {}).get(gate["id"])
            print(f"{gate['id']:10} due {gate['deadline']}  {gate['name']}")
            for c in gate["criteria"]:
                print(f"   - {c}")
            if g:
                print(f"   PASSED {g.get('passed_at')} {g.get('note', '')}")
            print()
        return 0

    if action != "pass":
        raise SystemExit("gate action must be list|pass")
    if gate_id not in gates:
        raise SystemExit(f"Unknown gate id. Choose: {', '.join(gates)}")
    progress.setdefault("gates", {})[gate_id] = {
        "passed_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
    }
    _save_progress(progress)
    print(f"✓ Gate {gate_id} ({gates[gate_id]['name']}) passed.")
    return 0


def cmd_review(curriculum: dict[str, Any], progress: dict[str, Any], today: date, text: str) -> int:
    week = _week_for(curriculum, today)
    week_n = week["week"] if week else None
    if not text:
        print("Weekly review prompts — answer in notes, then re-run with --text \"...\":")
        print("  1) What did I actually build?")
        print("  2) What concept is still fuzzy?")
        print("  3) What debt remains?")
        print("  4) One adjustment to next week's schedule?")
        NOTES_DIR.mkdir(parents=True, exist_ok=True)
        path = NOTES_DIR / f"review_week_{week_n or 'na'}_{today.isoformat()}.md"
        if not path.is_file():
            path.write_text(
                f"# Week {week_n} review — {today.isoformat()}\n\n"
                "## Built\n\n- \n\n## Still fuzzy\n\n- \n\n"
                "## Debt\n\n- \n\n## Adjustment\n\n- \n",
                encoding="utf-8",
            )
            print(f"\nCreated {path}")
        else:
            print(f"\nEdit {path}")
        return 0

    progress.setdefault("reviews", []).append(
        {
            "date": today.isoformat(),
            "week": week_n,
            "text": text,
        }
    )
    _save_progress(progress)
    print("Review saved to progress.json")
    return 0


def cmd_streak(curriculum: dict[str, Any], progress: dict[str, Any], today: date) -> int:
    sessions = sorted(_all_sessions(curriculum), key=lambda s: s["date"], reverse=True)
    streak = 0
    for session in sessions:
        day = date.fromisoformat(session["date"])
        if day > today:
            continue
        if day == today and _status_of(progress, session["id"]) != "done":
            continue
        if _status_of(progress, session["id"]) == "done":
            streak += 1
        else:
            break
    print(f"Completed-session streak (looking backward): {streak}")
    if streak == 0:
        print("PUSH: Do today's block. Streaks start with one session.")
    elif streak < 5:
        print("PUSH: Protect the streak — weekday 1h is sacred.")
    else:
        print("Strong streak. Don't break it for low-value scrolling.")
    return 0


def cmd_calendar(curriculum: dict[str, Any], progress: dict[str, Any], today: date) -> int:
    _print_header(curriculum, progress, today)
    print("\nNext 7 days:")
    for i in range(7):
        day = today + timedelta(days=i)
        session = _session_by_date(curriculum, day)
        if not session:
            print(f"  {day.isoformat()} {day.strftime('%a'):3}  —")
            continue
        st = _status_of(progress, session["id"])
        print(
            f"  {day.isoformat()} {day.strftime('%a'):3}  {session['hours']}h  "
            f"[{st:7}]  {session['title']}"
        )
    return 0


def cmd_graduate(curriculum: dict[str, Any], progress: dict[str, Any], today: date, score: int) -> int:
    rubric = [
        "Can design a multi-phase lab pipeline with soft/hard fail + teardown",
        "Can write resilient HTTP device clients (session, timeout, cancel)",
        "Can SSH non-interactively and explain cert vs password auth",
        "Understands MQTT retain + log-based protocol verification",
        "Can ship a thin Flask+SSE operator UI with safety separation",
        "Leaves artifacts/evidence, not just 'it worked on my machine'",
        "Can read and extend Drive Automation orchestrator confidently",
    ]
    print("Graduation rubric (self-score 0–2 each: 0=no, 1=partial, 2=yes):\n")
    for i, line in enumerate(rubric, 1):
        print(f"  {i}. {line}")
    print("\nPass threshold: ≥ 10 / 14")
    if score < 0:
        print("Re-run with: learning_coach.py graduate --score N")
        return 0
    if score < 10:
        print(f"Score {score}/14 — not yet. Finish capstone gaps; reassess next Sunday.")
        return 1
    progress["graduation"] = {
        "date": today.isoformat(),
        "score": score,
        "max": 14,
    }
    _save_progress(progress)
    print(f"✓ GRADUATED with {score}/14 on {today.isoformat()}.")
    print("Maintenance: 1h/week improving Drive Automation or your capstone.")
    return 0


def cmd_rules(curriculum: dict[str, Any]) -> int:
    print("Push rules:")
    for rule in curriculum["push_rules"]:
        print(f"  • {rule}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Lab Automation Pro Track coach")
    parser.add_argument(
        "command",
        nargs="?",
        default="today",
        choices=[
            "today",
            "week",
            "status",
            "done",
            "skip",
            "gate",
            "review",
            "streak",
            "calendar",
            "graduate",
            "rules",
        ],
    )
    parser.add_argument("rest", nargs="*", help="optional args / notes")
    parser.add_argument("--date", help="YYYY-MM-DD override (for planning)")
    parser.add_argument("--session", default="", help="session id for done/skip")
    parser.add_argument("--excused", action="store_true", help="skip without debt")
    parser.add_argument("--text", default="", help="review text")
    parser.add_argument("--score", type=int, default=-1, help="graduation score /14")
    parser.add_argument("--note", default="", help="note for done/gate pass")
    args = parser.parse_args(argv)

    curriculum = _load_json(CURRICULUM_PATH)
    progress = _load_progress(curriculum)
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "scripts" / "practice").mkdir(parents=True, exist_ok=True)

    today = date.fromisoformat(args.date) if args.date else date.today()

    if args.command == "today":
        return cmd_today(curriculum, progress, today)
    if args.command == "week":
        return cmd_week(curriculum, progress, today)
    if args.command == "status":
        return cmd_status(curriculum, progress, today)
    if args.command == "done":
        note = args.note or (" ".join(args.rest) if args.rest else "")
        return cmd_done(curriculum, progress, today, note, args.session)
    if args.command == "skip":
        reason = args.note or (" ".join(args.rest) if args.rest else "")
        return cmd_skip(curriculum, progress, today, reason, args.excused, args.session)
    if args.command == "gate":
        action = args.rest[0] if args.rest else "list"
        gate_id = args.rest[1] if len(args.rest) > 1 else ""
        note = args.note
        if action == "pass" and not gate_id:
            raise SystemExit("Usage: learning_coach.py gate pass gate-0 [--note '...']")
        return cmd_gate(curriculum, progress, action, gate_id, note)
    if args.command == "review":
        text = args.text or (" ".join(args.rest) if args.rest else "")
        return cmd_review(curriculum, progress, today, text)
    if args.command == "streak":
        return cmd_streak(curriculum, progress, today)
    if args.command == "calendar":
        return cmd_calendar(curriculum, progress, today)
    if args.command == "graduate":
        return cmd_graduate(curriculum, progress, today, args.score)
    if args.command == "rules":
        return cmd_rules(curriculum)
    return 0


if __name__ == "__main__":
    sys.exit(main())
