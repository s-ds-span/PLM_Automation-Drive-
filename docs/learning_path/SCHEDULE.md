# Schedule — Lab Automation Pro Track

**2026-08-03 → 2026-11-08** · 13 h/week · 1h weekdays / 4h Sat / 4h Sun

Use `python scripts/learning_coach.py week` for the live checklist.

## Week 1: Missing Semester + Python warmup

- **Phase:** 0 — Foundations
- **Dates:** 2026-08-03 → 2026-08-09
- **Week deadline:** **2026-08-09** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Shell, Git, and debugging feel boring; can navigate this repo without panic.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-08-03 Mon | 1 | Kickoff + map the system | Can explain in 5 bullets what Drive Automation does end-to-end |
| 2026-08-04 Tue | 1 | Shell Tools deep work | Completed shell exercises; used rg to find PIPELINE_PHASES |
| 2026-08-05 Wed | 1 | Command line + editors | Can edit files and run Python from terminal without friction |
| 2026-08-06 Thu | 1 | Git fluency | Can explain commit / branch / diff confidently |
| 2026-08-07 Fri | 1 | Debugging + repo tour | Know which file owns pipeline phases vs HTTP clients vs SSH |
| 2026-08-08 Sat | 4 | Saturday deep: Python Effective kickoff | Notes file exists; can point to teardown pattern in code |
| 2026-08-09 Sun | 4 | Sunday deep: pathlib + logging + week review | Week 1 coach review submitted; tiny JSONL script works |

Resources:
- https://missing.csail.mit.edu/
- https://automatetheboringstuff.com/ (only if basics feel weak)
- This repo README.md — skim pipeline diagram

## Week 2: Effective Python + concurrency basics

- **Phase:** 0 — Foundations
- **Dates:** 2026-08-10 → 2026-08-16
- **Week deadline:** **2026-08-16** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Write clear modules; understand threads enough to read TelemetryLogger-style code.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-08-10 Mon | 1 | Dataclasses + typing | Can sketch a dataclass for a 'RunRequest' with iterations + flags |
| 2026-08-11 Tue | 1 | Exceptions & robustness | List 5 failure modes a lab orchestrator must handle |
| 2026-08-12 Wed | 1 | Threading mental model | Can explain why background sampling needs a stop flag |
| 2026-08-13 Thu | 1 | Packaging mindset | Imports work from venv; understand package layout |
| 2026-08-14 Fri | 1 | Repo mapping worksheet | module_map.md has ≥10 entries |
| 2026-08-15 Sat | 4 | Saturday: build toy 'session client' | Script always logs out even when middle step raises |
| 2026-08-16 Sun | 4 | Sunday: harden + Phase 0 gate | Phase 0 gate passed in notes; tests green |

Resources:
- https://effectivepython.com/
- https://docs.python.org/3/howto/logging.html
- https://realpython.com/ (threading / dataclasses)

## Week 3: HTTP mastery + requests

- **Phase:** 1 — HTTP clients
- **Dates:** 2026-08-17 → 2026-08-23
- **Week deadline:** **2026-08-23** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Own session-based HTTP clients with clear errors and timeouts.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-08-17 Mon | 1 | HTTP core | Flashcard-ready: what 409 means for EV loads armed |
| 2026-08-18 Tue | 1 | Cookies & sessions | Can explain why EvClient keeps a Session |
| 2026-08-19 Wed | 1 | Read clients.py deeply | Notes list EV vs LB2 API surface you care about |
| 2026-08-20 Thu | 1 | Timeouts & errors | Written rule for default timeouts you'll use |
| 2026-08-21 Fri | 1 | Mock device sketch | Route list ready for Saturday build |
| 2026-08-22 Sat | 4 | Saturday: fake device + client | Demo script run recorded in notes (commands + outcome) |
| 2026-08-23 Sun | 4 | Sunday: cancel + 409 handling | Cancel path works; Phase 1 checkpoint notes done |

Resources:
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Overview
- https://developer.mozilla.org/en-US/docs/Web/HTTP/Cookies
- https://requests.readthedocs.io/
- https://realpython.com/python-requests/

## Week 4: Client patterns + polling loops

- **Phase:** 1 — HTTP clients
- **Dates:** 2026-08-24 → 2026-08-30
- **Week deadline:** **2026-08-30** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Polling, readiness waits, and availability checks like the real lab.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-08-24 Mon | 1 | wait_ready patterns | Can implement wait_until(predicate, timeout) from scratch |
| 2026-08-25 Tue | 1 | Release It! kickoff | 3 pattern → code mappings written |
| 2026-08-26 Wed | 1 | Idempotency & login | Decision table: busy / error / ready |
| 2026-08-27 Thu | 1 | Refactor practice client | Practice client has shared _request helper |
| 2026-08-28 Fri | 1 | Read orchestrator boot phase | Event list for boot phase in notes |
| 2026-08-29 Sat | 4 | Saturday: readiness library | Tests prove timeout raises; success returns value |
| 2026-08-30 Sun | 4 | Sunday: Phase 1 gate | Phase 1 gate passed |

Resources:
- drive_automation/clients.py
- README Phase 4–5 (boot / handshake)
- Release It! — start reading (timeouts chapter)

## Week 5: Pipelines & soft vs hard fail

- **Phase:** 2 — Orchestration
- **Dates:** 2026-08-31 → 2026-09-06
- **Week deadline:** **2026-09-06** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Design phase pipelines with skip rules and severity levels.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-08-31 Mon | 1 | FSM thinking | Diagram exists with soft/hard labels |
| 2026-09-01 Tue | 1 | Saga & compensating actions | Written: why decommission is NOT in one-click |
| 2026-09-02 Wed | 1 | Circuit breaker | 1 paragraph design note |
| 2026-09-03 Thu | 1 | Orchestrator skim | Can explain Stop button behavior |
| 2026-09-04 Fri | 1 | Event contract | Event cheatsheet started |
| 2026-09-05 Sat | 4 | Saturday: toy 5-phase orchestrator | Run produces JSONL; teardown always runs |
| 2026-09-06 Sun | 4 | Sunday: cancel + skips | Cancel exits cleanly; optional phase skip works |

Resources:
- Release It! (continue)
- https://martinfowler.com/bliki/CircuitBreaker.html
- https://martinfowler.com/articles/patterns-of-distributed-systems/saga.html
- README phase table (skip when)

## Week 6: Dynamic control loops + artifacts

- **Phase:** 2 — Orchestration
- **Dates:** 2026-09-07 → 2026-09-13
- **Week deadline:** **2026-09-13** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Build measure→act→verify loops (throttle-style) and package evidence.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-09-07 Mon | 1 | Throttle loop theory | Can explain baseline → drop → sustain → recover in own words |
| 2026-09-08 Tue | 1 | Read load_controller | Notes: what inputs drive LB2 steps |
| 2026-09-09 Wed | 1 | Artifacts | Know what a 'good run package' contains |
| 2026-09-10 Thu | 1 | JAMA evidence idea | 1 paragraph: evidence vs just logs |
| 2026-09-11 Fri | 1 | Design your loop | Pseudo-code ready |
| 2026-09-12 Sat | 4 | Saturday: mock throttle controller | Simulated run reaches 0 then recovers toward baseline |
| 2026-09-13 Sun | 4 | Sunday: Phase 2 gate | Phase 2 gate passed |

Resources:
- README Phases 7–11
- drive_automation/load_controller.py
- drive_automation/artifacts.py
- Release It! finish core stability chapters

## Week 7: SSH fundamentals + OpenSSH config

- **Phase:** 3 — Remote / SSH
- **Dates:** 2026-09-14 → 2026-09-20
- **Week deadline:** **2026-09-20** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Non-interactive SSH you trust; keys and config files second nature.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-09-14 Mon | 1 | SSH mental model | Host alias pattern written for wall/gateway |
| 2026-09-15 Tue | 1 | Keys | Can explain key vs password vs agent |
| 2026-09-16 Wed | 1 | Non-interactive commands | Script pattern: (rc, out, err) tuple |
| 2026-09-17 Thu | 1 | Timeouts & buffering | Know why remote CLIs 'hang' with no output |
| 2026-09-18 Fri | 1 | Read gateway.py (part 1) | Flowchart: auth selection auto/cert/password |
| 2026-09-19 Sat | 4 | Saturday: ssh_run library | Wrapper returns rc/out/err; timeout covered |
| 2026-09-20 Sun | 4 | Sunday: deepen + review | Week 7 complete; notes compare to gateway.py |

Resources:
- https://wiki.archlinux.org/title/OpenSSH
- https://man.openbsd.org/ssh
- https://man.openbsd.org/ssh_config
- Missing Semester shell/SSH revisit

## Week 8: SSH certificates + Paramiko + deploy

- **Phase:** 3 — Remote / SSH
- **Dates:** 2026-09-21 → 2026-09-27
- **Week deadline:** **2026-09-27** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Understand CA cert auth used on gateway; Paramiko fallback; env deploy pattern.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-09-21 Mon | 1 | SSH certificates | Can explain TrustedUserCAKeys in one paragraph |
| 2026-09-22 Tue | 1 | Paramiko | Know when cert path vs paramiko path fires |
| 2026-09-23 Wed | 1 | Multi-wall env model | Explain WALL_IP → CAPTURE_SSH_HOST |
| 2026-09-24 Thu | 1 | Deploy scripts read | Checklist: what deploy overwrites |
| 2026-09-25 Fri | 1 | Security hygiene | Personal 'never commit secrets' checklist |
| 2026-09-26 Sat | 4 | Saturday: remote command runner + fake deploy | Renderer produces a config.env-like file from template |
| 2026-09-27 Sun | 4 | Sunday: Phase 3 gate | Phase 3 gate passed |

Resources:
- https://smallstep.com/docs/ssh/
- https://docs.paramiko.org/
- walls/README.md + setup_wall.sh (read)
- man ssh-keygen CERTIFICATES section

## Week 9: MQTT Essentials

- **Phase:** 4 — Protocols
- **Dates:** 2026-09-28 → 2026-10-04
- **Week deadline:** **2026-10-04** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Pub/sub, QoS, retained messages — enough to reason about commission shadows.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-09-28 Mon | 1 | MQTT parts 1–2 | Diagram pub/sub with broker in middle |
| 2026-09-29 Tue | 1 | Parts 3–4 | Can list connection lifecycle steps |
| 2026-09-30 Wed | 1 | Parts 5–6 | QoS choice note for retained config-like data |
| 2026-10-01 Thu | 1 | Parts 7–8 retained | Explain retain in commission context |
| 2026-10-02 Fri | 1 | Parts 9–10 | Essentials series complete |
| 2026-10-03 Sat | 4 | Saturday: hands-on MQTT | Demo: retain visible to late subscriber |
| 2026-10-04 Sun | 4 | Sunday: commission mapping | Sequence diagram exists |

Resources:
- https://www.hivemq.com/mqtt/
- https://www.hivemq.com/blog/mqtt-essentials-part-1-introducing-mqtt/
- https://www.hivemq.com/blog/how-to-get-started-with-mqtt/

## Week 10: UART/serial + CAN/Modbus literacy

- **Phase:** 4 — Protocols
- **Dates:** 2026-10-05 → 2026-10-11
- **Week deadline:** **2026-10-11** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Log-scrape verification mindset; enough bus literacy to trust SBX evidence.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-10-05 Mon | 1 | UART basics | Know baud/framing; why 'no frames yet' can be OK pre-commission |
| 2026-10-06 Tue | 1 | pySerial practice | Script parses lines from a sample log file |
| 2026-10-07 Wed | 1 | CAN intro | Explain ID + payload at high level |
| 2026-10-08 Thu | 1 | Modbus intro | Map to 'AssignDynamicBusId + Ack' style verifies |
| 2026-10-09 Fri | 1 | Read sbx_verify | Cheatsheet of pairs you verify |
| 2026-10-10 Sat | 4 | Saturday: log verifier | Verifier outputs table + cases JSON |
| 2026-10-11 Sun | 4 | Sunday: Phase 4 gate | Phase 4 gate passed |

Resources:
- https://pyserial.readthedocs.io/
- https://www.csselectronics.com/pages/can-bus-simple-intro-tutorial
- https://www.simplymodbus.ca/FAQ.htm
- drive_automation/sbx_verify.py + sbx_decode.py

## Week 11: Flask operator UI + SSE

- **Phase:** 5 — Productize
- **Dates:** 2026-10-12 → 2026-10-18
- **Week deadline:** **2026-10-18** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Thin dashboard over an orchestrator with live phase events.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-10-12 Mon | 1 | Flask Mega-Tutorial start | Local hello-world Flask runs |
| 2026-10-13 Tue | 1 | JSON APIs in Flask | API returns JSON status |
| 2026-10-14 Wed | 1 | SSE theory | Can explain SSE vs websocket for lab UI |
| 2026-10-15 Thu | 1 | Read dashboard/app.py | Route → responsibility table |
| 2026-10-16 Fri | 1 | UX rules | Rules written |
| 2026-10-17 Sat | 4 | Saturday: dashboard over toy orchestrator | Browser shows live phases; Stop works |
| 2026-10-18 Sun | 4 | Sunday: artifacts endpoint + review | Decommission is separate route; Phase 5 checkpoint ok |

Resources:
- https://blog.miguelgrinberg.com/post/the-flask-mega-tutorial-part-i-hello-world
- https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events
- dashboard/app.py (read)

## Week 12: pytest + hardening the practice stack

- **Phase:** 6 — Testing & rigor
- **Dates:** 2026-10-19 → 2026-10-25
- **Week deadline:** **2026-10-25** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Test clients and orchestrator pieces; treat evidence as a feature.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-10-19 Mon | 1 | pytest fixtures | Rewrite one practice test using fixtures |
| 2026-10-20 Tue | 1 | Testing clients | Client tests without real network |
| 2026-10-21 Wed | 1 | Testing orchestrator | ≥5 orchestrator tests green |
| 2026-10-22 Thu | 1 | Evidence quality | DoD checklist written |
| 2026-10-23 Fri | 1 | Read report/jama | Know how cases get IDs/titles |
| 2026-10-24 Sat | 4 | Saturday: test suite day | 20+ tests passing |
| 2026-10-25 Sun | 4 | Sunday: Phase 6 checkpoint | Test doc + week review done |

Resources:
- https://docs.pytest.org/
- https://realpython.com/pytest-python-testing/
- drive_automation/jama_cases.py

## Week 13: Capstone week A — design a mini lab pipeline

- **Phase:** Capstone
- **Dates:** 2026-10-26 → 2026-11-01
- **Week deadline:** **2026-11-01** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Design and start building an end-to-end mini system with real structure.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-10-26 Mon | 1 | Pick capstone scope | Design doc merged into notes/capstone_design.md |
| 2026-10-27 Tue | 1 | Skeleton repo/package | Skeleton runs --help |
| 2026-10-28 Wed | 1 | HTTP client module | Client tests green |
| 2026-10-29 Thu | 1 | SSH/mock gateway module | Interface stable |
| 2026-10-30 Fri | 1 | Orchestrator skeleton | Empty phases emit events |
| 2026-10-31 Sat | 4 | Saturday: implement core phases | Happy-path run works end-to-end on mocks |
| 2026-11-01 Sun | 4 | Sunday: mid-capstone review | Gap list ≤5 items |

Resources:
- All prior notes
- This repo as reference architecture (do not copy blindly — understand)

## Week 14: Capstone week B — ship + Drive Automation fluency

- **Phase:** Capstone
- **Dates:** 2026-11-02 → 2026-11-08
- **Week deadline:** **2026-11-08** (complete all 7 sessions)
- **Hours:** 13
- **Goal:** Finish capstone; prove you can read and extend Drive Automation confidently.

| Day | Hours | Session | Done when |
|-----|-------|---------|-----------|
| 2026-11-02 Mon | 1 | Capstone: cancel + soft fail | All failure modes demoable |
| 2026-11-03 Tue | 1 | Capstone: dashboard polish | UI usable by a teammate |
| 2026-11-04 Wed | 1 | Capstone: artifacts + cases | Artifact download works |
| 2026-11-05 Thu | 1 | Drive Automation deep dive | Annotation note completed |
| 2026-11-06 Fri | 1 | Propose one real improvement | RFC in notes/rfc_drive_automation.md |
| 2026-11-07 Sat | 4 | Saturday: ship capstone | Capstone README + demo script exist; tests green |
| 2026-11-08 Sun | 4 | Sunday: graduation | Graduation rubric ≥ pass score |

Resources:
- drive_automation/orchestrator.py (targeted deep reads)
- Your capstone

## Phase gate deadlines

| Gate | Name | Deadline | Criteria (summary) |
|------|------|----------|--------------------|
| `gate-0` | Foundations | **2026-08-16** | module_map.md ≥ 10 modules; toy session client always logouts in finally… |
| `gate-1` | HTTP clients | **2026-08-30** | fake device + client with poll + cancel; wait_until tested… |
| `gate-2` | Orchestration | **2026-09-13** | 5-phase orchestrator with JSONL events; throttle simulator with recover… |
| `gate-3` | SSH / deploy | **2026-09-27** | ssh_run wrapper with timeout; env renderer… |
| `gate-4` | Protocols | **2026-10-11** | MQTT retain demo; log verifier with seen/not-seen cases… |
| `gate-5` | Product UI | **2026-10-18** | Flask + SSE live phases; Stop works… |
| `gate-6` | Testing | **2026-10-25** | ≥20 practice tests green; DoD checklist for a phase |
| `gate-grad` | Graduation | **2026-11-08** | capstone happy path + failure modes; Drive Automation annotation notes… |

