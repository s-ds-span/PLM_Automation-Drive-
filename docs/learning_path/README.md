# Lab Automation Pro Track

A **14-week deadline system** that turns the learning path + resources into a schedule you can finish — with a coach CLI that nags you when you fall behind.

**Time budget (yours):** Mon–Fri **1h**, Sat **4h**, Sun **4h** → **13 h/week**  
**Program window:** **2026-08-03 → 2026-11-08**  
**End goal:** Design and build lab automation like Drive Automation (HTTP clients, orchestration, SSH, MQTT/serial evidence, operator UI, artifacts).

---

## Daily ritual (this is the system)

```bash
# 1) Start of every study block
python scripts/learning_coach.py

# 2) Do the tasks it prints (timer on)

# 3) End of block
python scripts/learning_coach.py done
# or: python scripts/learning_coach.py done "built wait_until"
```

Sunday last 30 minutes:

```bash
python scripts/learning_coach.py review
```

Weekly health check (any day):

```bash
python scripts/learning_coach.py status
python scripts/learning_coach.py week
python scripts/learning_coach.py calendar
```

---

## Hard deadlines (put these on your calendar)

| Gate | Name | Due (Sunday) | After week |
|------|------|--------------|------------|
| `gate-0` | Foundations | **2026-08-16** | 2 |
| `gate-1` | HTTP clients | **2026-08-30** | 4 |
| `gate-2` | Orchestration | **2026-09-13** | 6 |
| `gate-3` | SSH / deploy | **2026-09-27** | 8 |
| `gate-4` | Protocols | **2026-10-11** | 10 |
| `gate-5` | Product UI | **2026-10-18** | 11 |
| `gate-6` | Testing | **2026-10-25** | 12 |
| `gate-grad` | Graduation | **2026-11-08** | 14 |

Pass a gate when criteria are met:

```bash
python scripts/learning_coach.py gate list
python scripts/learning_coach.py gate pass gate-0 --note "module_map + toy client done"
```

Full week-by-week plan: [`SCHEDULE.md`](SCHEDULE.md)  
Machine-readable sessions: [`curriculum.json`](curriculum.json)  
Regenerate curriculum: `python docs/learning_path/build_curriculum.py`

---

## Push rules (non-negotiable)

1. Study block is sacred — move it earlier in the day, don’t delete it.
2. Mark `done` the **same day** (coach tracks debt from unmarked past sessions).
3. Miss a weekday → catch up on the **next weekend** (don’t silently skip forever).
4. **Max debt = 3 sessions.** If debt > 3, Saturday is burn-down only.
5. Sunday review is mandatory (last 30 min).
6. Phase gates are hard — don’t mentally “start next phase” until gate is passed in the coach.
7. One practice script > three passive videos. **Saturdays are build days.**

```bash
python scripts/learning_coach.py rules
```

---

## Pre-start (now → 2026-08-02)

Use this week to set the system up so Day 1 is frictionless:

- [ ] Calendar: recurring Mon–Fri 1h + Sat 4h + Sun 4h through **Nov 8**
- [ ] Add gate Sundays as all-day “DEADLINE” events
- [ ] Get **Effective Python** + **Release It!**
- [ ] Bookmark Missing Semester + HiveMQ MQTT Essentials
- [ ] Run `python scripts/learning_coach.py` once (shows pre-start checklist)

---

## Files

| Path | Role |
|------|------|
| `scripts/learning_coach.py` | Daily coach / nag / progress |
| `docs/learning_path/curriculum.json` | All sessions + deadlines |
| `docs/learning_path/progress.json` | Your completion state (created on first run) |
| `docs/learning_path/notes/` | Reviews, maps, RFC, gates evidence |
| `scripts/practice/` | Weekly build artifacts (`w03_…`, `w05_…`, …) |

---

## If you fall behind

```bash
python scripts/learning_coach.py status   # see CRITICAL / BEHIND
```

- **BEHIND:** do today’s session, then Saturday morning = debt queue first.  
- **CRITICAL:** stop optional reading; only close debt + current week deadline.  
- Excused miss (travel/health): `python scripts/learning_coach.py skip --excused "sick"`  
- Unexcused miss: `skip` without `--excused` → still debt until you `done --session …`

---

## Graduation

On **2026-11-08** (or when capstone is ready):

```bash
python scripts/learning_coach.py graduate --score 12
```

Rubric is 7 items × 0–2 points; **pass ≥ 10/14**.
