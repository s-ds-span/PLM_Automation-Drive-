#!/usr/bin/env python3
"""Generate curriculum.json from compact week definitions."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

START = date(2026, 8, 3)  # Monday
WEEKDAY_H = 1
WEEKEND_H = 4

# Each week: title, phase, goal, resources, and 7 day plans (Mon..Sun).
# Day plan: title, tasks[], done_when, optional resources_focus
WEEKS: list[dict] = [
    {
        "title": "Missing Semester + Python warmup",
        "phase": "0 — Foundations",
        "goal": "Shell, Git, and debugging feel boring; can navigate this repo without panic.",
        "resources": [
            "https://missing.csail.mit.edu/",
            "https://automatetheboringstuff.com/ (only if basics feel weak)",
            "This repo README.md — skim pipeline diagram",
        ],
        "days": [
            (
                "Kickoff + map the system",
                [
                    "Read docs/learning_path/README.md (system rules)",
                    "Skim repo README phases 1–11 once",
                    "Install: note-taking app + bookmark resource list",
                    "Missing Semester: watch/read Shell Tools lecture",
                ],
                "Can explain in 5 bullets what Drive Automation does end-to-end",
            ),
            (
                "Shell Tools deep work",
                [
                    "Finish Missing Semester shell lecture + exercises",
                    "Practice: pipes, find/rg, redirects on this repo",
                ],
                "Completed shell exercises; used rg to find PIPELINE_PHASES",
            ),
            (
                "Command line + editors",
                [
                    "Missing Semester: editors + command-line environment",
                    "Set up a comfortable terminal workflow for daily study",
                ],
                "Can edit files and run Python from terminal without friction",
            ),
            (
                "Git fluency",
                [
                    "Missing Semester: Git lecture + exercises",
                    "Practice: status, diff, log, branch on a throwaway branch (no push required)",
                ],
                "Can explain commit / branch / diff confidently",
            ),
            (
                "Debugging + repo tour",
                [
                    "Missing Semester: debugging + profiling (skim profiling)",
                    "Trace: run_test.py → orchestrator imports",
                ],
                "Know which file owns pipeline phases vs HTTP clients vs SSH",
            ),
            (
                "Saturday deep: Python Effective kickoff",
                [
                    "Effective Python: read items on exceptions + context managers",
                    "Map those ideas to try/finally teardown in orchestrator (read the finally block)",
                    "Write 1 page notes: 'what I will copy into my own orchestrators'",
                ],
                "Notes file exists; can point to teardown pattern in code",
            ),
            (
                "Sunday deep: pathlib + logging + week review",
                [
                    "Effective Python / Real Python: pathlib + logging howto",
                    "Tiny script: walk a folder, write a JSONL log of file names",
                    "Run: python scripts/learning_coach.py review — write week 1 retrospective",
                ],
                "Week 1 coach review submitted; tiny JSONL script works",
            ),
        ],
    },
    {
        "title": "Effective Python + concurrency basics",
        "phase": "0 — Foundations",
        "goal": "Write clear modules; understand threads enough to read TelemetryLogger-style code.",
        "resources": [
            "https://effectivepython.com/",
            "https://docs.python.org/3/howto/logging.html",
            "https://realpython.com/ (threading / dataclasses)",
        ],
        "days": [
            (
                "Dataclasses + typing",
                [
                    "Read Effective Python items on dataclasses / annotations (or Real Python dataclasses)",
                    "Read drive_automation/config.py — how config is modeled",
                ],
                "Can sketch a dataclass for a 'RunRequest' with iterations + flags",
            ),
            (
                "Exceptions & robustness",
                [
                    "Effective Python: exception items + robustness chapter skim",
                    "Read DashboardError / GatewayError usage patterns in clients.py + gateway.py (skim)",
                ],
                "List 5 failure modes a lab orchestrator must handle",
            ),
            (
                "Threading mental model",
                [
                    "Real Python threading primer OR Fluent Python concurrency chapter (threads only)",
                    "Find threading usage in orchestrator/telemetry",
                ],
                "Can explain why background sampling needs a stop flag",
            ),
            (
                "Packaging mindset",
                [
                    "Read drive_automation/ as a package (__init__, imports)",
                    "Practice: python -c 'from drive_automation.config import ...' from repo root",
                ],
                "Imports work from venv; understand package layout",
            ),
            (
                "Repo mapping worksheet",
                [
                    "Fill docs/learning_path/notes/module_map.md: file → responsibility (10 modules)",
                ],
                "module_map.md has ≥10 entries",
            ),
            (
                "Saturday: build toy 'session client'",
                [
                    "Write scripts/practice/w02_session_client.py: fake login/logout with finally",
                    "Add timeouts + custom errors",
                    "Effective Python: revisit concurrency/robustness items while building",
                ],
                "Script always logs out even when middle step raises",
            ),
            (
                "Sunday: harden + Phase 0 gate",
                [
                    "Add CLI argparse to toy client; write 3 pytest tests",
                    "Phase 0 gate: explain module_map aloud / in notes for 10 minutes",
                    "Coach review week 2",
                ],
                "Phase 0 gate passed in notes; tests green",
            ),
        ],
    },
    {
        "title": "HTTP mastery + requests",
        "phase": "1 — HTTP clients",
        "goal": "Own session-based HTTP clients with clear errors and timeouts.",
        "resources": [
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Overview",
            "https://developer.mozilla.org/en-US/docs/Web/HTTP/Cookies",
            "https://requests.readthedocs.io/",
            "https://realpython.com/python-requests/",
        ],
        "days": [
            (
                "HTTP core",
                [
                    "MDN HTTP overview — methods, status codes, headers",
                    "Memorize: 401, 404, 409, 5xx meanings for lab devices",
                ],
                "Flashcard-ready: what 409 means for EV loads armed",
            ),
            (
                "Cookies & sessions",
                [
                    "MDN Cookies",
                    "requests Session docs — cookies across calls",
                ],
                "Can explain why EvClient keeps a Session",
            ),
            (
                "Read clients.py deeply",
                [
                    "Read drive_automation/clients.py BaseClient + login paths",
                    "Annotate (in notes) every public method EV vs LB2",
                ],
                "Notes list EV vs LB2 API surface you care about",
            ),
            (
                "Timeouts & errors",
                [
                    "Real Python requests tutorial — errors, timeouts",
                    "Design rule: never hang forever on device HTTP",
                ],
                "Written rule for default timeouts you'll use",
            ),
            (
                "Mock device sketch",
                [
                    "Sketch Flask routes for /login and /api/job on paper or notes",
                ],
                "Route list ready for Saturday build",
            ),
            (
                "Saturday: fake device + client",
                [
                    "Build scripts/practice/w03_fake_device.py (Flask) + w03_client.py",
                    "Client: login → start job → poll until done → logout in finally",
                    "Inject a failure mid-job; prove logout still runs",
                ],
                "Demo script run recorded in notes (commands + outcome)",
            ),
            (
                "Sunday: cancel + 409 handling",
                [
                    "Add cancel/stop to fake device; client must exit cleanly",
                    "Simulate 409 conflict; client surfaces clear error",
                    "Coach review; compare to EvClient load arming comments in README",
                ],
                "Cancel path works; Phase 1 checkpoint notes done",
            ),
        ],
    },
    {
        "title": "Client patterns + polling loops",
        "phase": "1 — HTTP clients",
        "goal": "Polling, readiness waits, and availability checks like the real lab.",
        "resources": [
            "drive_automation/clients.py",
            "README Phase 4–5 (boot / handshake)",
            "Release It! — start reading (timeouts chapter)",
        ],
        "days": [
            (
                "wait_ready patterns",
                [
                    "Find wait_ready / availability logic in clients + orchestrator",
                    "Write notes: poll interval vs timeout vs fail-fast conditions",
                ],
                "Can implement wait_until(predicate, timeout) from scratch",
            ),
            (
                "Release It! kickoff",
                [
                    "Release It! : stability patterns / timeouts chapters",
                    "Map 3 patterns to Drive Automation behavior",
                ],
                "3 pattern → code mappings written",
            ),
            (
                "Idempotency & login",
                [
                    "Think through double-login, already-busy LB2",
                    "Read README 'LB2 unavailable' section",
                ],
                "Decision table: busy / error / ready",
            ),
            (
                "Refactor practice client",
                [
                    "Extract BaseClient pattern into your practice code (DRY)",
                ],
                "Practice client has shared _request helper",
            ),
            (
                "Read orchestrator boot phase",
                [
                    "Read orchestrator Phase 4 boot section (login + ready)",
                    "List events emitted",
                ],
                "Event list for boot phase in notes",
            ),
            (
                "Saturday: readiness library",
                [
                    "scripts/practice/w04_wait.py: generic wait_until + demos",
                    "Unit tests for timeout and success paths",
                    "Continue Release It!",
                ],
                "Tests prove timeout raises; success returns value",
            ),
            (
                "Sunday: Phase 1 gate",
                [
                    "Rebuild Ev-like client against fake device using wait_until",
                    "Phase 1 gate checklist in notes",
                    "Coach review week 4",
                ],
                "Phase 1 gate passed",
            ),
        ],
    },
    {
        "title": "Pipelines & soft vs hard fail",
        "phase": "2 — Orchestration",
        "goal": "Design phase pipelines with skip rules and severity levels.",
        "resources": [
            "Release It! (continue)",
            "https://martinfowler.com/bliki/CircuitBreaker.html",
            "https://martinfowler.com/articles/patterns-of-distributed-systems/saga.html",
            "README phase table (skip when)",
        ],
        "days": [
            (
                "FSM thinking",
                [
                    "Write PIPELINE_PHASES as a state diagram in notes (mermaid ok)",
                    "Mark soft vs hard fails from README",
                ],
                "Diagram exists with soft/hard labels",
            ),
            (
                "Saga & compensating actions",
                [
                    "Read Fowler Saga article",
                    "Map: recover, teardown, decommission-separate",
                ],
                "Written: why decommission is NOT in one-click",
            ),
            (
                "Circuit breaker",
                [
                    "Fowler Circuit Breaker (short)",
                    "Where would it apply to gateway SSH storms?",
                ],
                "1 paragraph design note",
            ),
            (
                "Orchestrator skim",
                [
                    "Skim orchestrator for phase transitions + _stop_check",
                    "Note how cancel works",
                ],
                "Can explain Stop button behavior",
            ),
            (
                "Event contract",
                [
                    "List 15 event names from README; group by phase",
                ],
                "Event cheatsheet started",
            ),
            (
                "Saturday: toy 5-phase orchestrator",
                [
                    "scripts/practice/w05_orchestrator.py: boot→work→verify→recover→teardown",
                    "JSONL event log; soft verify can fail without abort",
                    "Hard fail on boot; always teardown in finally",
                    "Release It! reading block (1h)",
                ],
                "Run produces JSONL; teardown always runs",
            ),
            (
                "Sunday: cancel + skips",
                [
                    "Add cancel flag mid-loop; skip flags for optional phases",
                    "Compare design to PIPELINE_PHASES + commission checkbox",
                    "Coach review",
                ],
                "Cancel exits cleanly; optional phase skip works",
            ),
        ],
    },
    {
        "title": "Dynamic control loops + artifacts",
        "phase": "2 — Orchestration",
        "goal": "Build measure→act→verify loops (throttle-style) and package evidence.",
        "resources": [
            "README Phases 7–11",
            "drive_automation/load_controller.py",
            "drive_automation/artifacts.py",
            "Release It! finish core stability chapters",
        ],
        "days": [
            (
                "Throttle loop theory",
                [
                    "README aggregate/throttle/verify/recover — outline the feedback loop",
                ],
                "Can explain baseline → drop → sustain → recover in own words",
            ),
            (
                "Read load_controller",
                [
                    "Read load_controller.py + load_selection.py (skim selection math)",
                ],
                "Notes: what inputs drive LB2 steps",
            ),
            (
                "Artifacts",
                [
                    "Read artifacts.py; README teardown artifact section",
                ],
                "Know what a 'good run package' contains",
            ),
            (
                "JAMA evidence idea",
                [
                    "Skim jama_cases.py — why emit cases",
                ],
                "1 paragraph: evidence vs just logs",
            ),
            (
                "Design your loop",
                [
                    "Pseudo-code Saturday's aggregate-until-metric-drops controller",
                ],
                "Pseudo-code ready",
            ),
            (
                "Saturday: mock throttle controller",
                [
                    "scripts/practice/w06_throttle.py: mock charge_rate_a + step load",
                    "Emit events: throttle_engaged, verify, recovery_step",
                    "Zip a mini artifact folder at end",
                ],
                "Simulated run reaches 0 then recovers toward baseline",
            ),
            (
                "Sunday: Phase 2 gate",
                [
                    "Add sustain timeout + recovery timeout paths",
                    "Phase 2 gate: present toy orchestrator + throttle to notes/video",
                    "Coach review; finish Release It! core chapters",
                ],
                "Phase 2 gate passed",
            ),
        ],
    },
    {
        "title": "SSH fundamentals + OpenSSH config",
        "phase": "3 — Remote / SSH",
        "goal": "Non-interactive SSH you trust; keys and config files second nature.",
        "resources": [
            "https://wiki.archlinux.org/title/OpenSSH",
            "https://man.openbsd.org/ssh",
            "https://man.openbsd.org/ssh_config",
            "Missing Semester shell/SSH revisit",
        ],
        "days": [
            (
                "SSH mental model",
                [
                    "Arch Wiki OpenSSH — client basics + config",
                    "Document your lab hosts in a personal ssh config snippet (notes)",
                ],
                "Host alias pattern written for wall/gateway",
            ),
            (
                "Keys",
                [
                    "ssh-keygen man page (key types); understand IdentitiesOnly",
                    "README Gateway SSH section — match to theory",
                ],
                "Can explain key vs password vs agent",
            ),
            (
                "Non-interactive commands",
                [
                    "Practice locally: ssh to a reachable host OR use a VM/container if no lab",
                    "Capture exit codes; parse stdout",
                ],
                "Script pattern: (rc, out, err) tuple",
            ),
            (
                "Timeouts & buffering",
                [
                    "Read panel_ratings.py comments about SSH pipe buffering (grep buffering)",
                    "Notes: line-buffered remote commands",
                ],
                "Know why remote CLIs 'hang' with no output",
            ),
            (
                "Read gateway.py (part 1)",
                [
                    "Read gateway.py top half — cert vs paramiko paths",
                ],
                "Flowchart: auth selection auto/cert/password",
            ),
            (
                "Saturday: ssh_run library",
                [
                    "scripts/practice/w07_ssh_run.py: subprocess ssh wrapper with timeout",
                    "Tests with mocked subprocess",
                    "Arch Wiki deep dive 1h",
                ],
                "Wrapper returns rc/out/err; timeout covered",
            ),
            (
                "Sunday: deepen + review",
                [
                    "Add Retry-on-network-fail (limited) to wrapper",
                    "Map wrapper to GatewayShell methods",
                    "Coach review",
                ],
                "Week 7 complete; notes compare to gateway.py",
            ),
        ],
    },
    {
        "title": "SSH certificates + Paramiko + deploy",
        "phase": "3 — Remote / SSH",
        "goal": "Understand CA cert auth used on gateway; Paramiko fallback; env deploy pattern.",
        "resources": [
            "https://smallstep.com/docs/ssh/",
            "https://docs.paramiko.org/",
            "walls/README.md + setup_wall.sh (read)",
            "man ssh-keygen CERTIFICATES section",
        ],
        "days": [
            (
                "SSH certificates",
                [
                    "Smallstep SSH certs guide OR ssh-keygen CERTIFICATES",
                    "README: id_ecdsa + id_ecdsa-cert.pub expectations",
                ],
                "Can explain TrustedUserCAKeys in one paragraph",
            ),
            (
                "Paramiko",
                [
                    "Paramiko docs — SSHClient basics",
                    "Read password/agent fallback in gateway.py",
                ],
                "Know when cert path vs paramiko path fires",
            ),
            (
                "Multi-wall env model",
                [
                    "walls/README.md + config.env.example mapping table",
                ],
                "Explain WALL_IP → CAPTURE_SSH_HOST",
            ),
            (
                "Deploy scripts read",
                [
                    "Read setup_wall.sh / deploy_to_lab.sh at high level",
                    "List dangerous vs safe operations",
                ],
                "Checklist: what deploy overwrites",
            ),
            (
                "Security hygiene",
                [
                    "Rules for secrets in walls/*.env; why gitignored",
                    "Mozilla OpenSSH guidelines skim",
                ],
                "Personal 'never commit secrets' checklist",
            ),
            (
                "Saturday: remote command runner + fake deploy",
                [
                    "Extend practice SSH tool: run remote 'echo' template commands",
                    "scripts/practice/w08_render_env.py: render config from wall env (like render_config_env.sh)",
                    "Paramiko optional path if no OpenSSH cert lab",
                ],
                "Renderer produces a config.env-like file from template",
            ),
            (
                "Sunday: Phase 3 gate",
                [
                    "Write gateway auth decision test matrix (table)",
                    "Phase 3 gate notes; coach review",
                ],
                "Phase 3 gate passed",
            ),
        ],
    },
    {
        "title": "MQTT Essentials",
        "phase": "4 — Protocols",
        "goal": "Pub/sub, QoS, retained messages — enough to reason about commission shadows.",
        "resources": [
            "https://www.hivemq.com/mqtt/",
            "https://www.hivemq.com/blog/mqtt-essentials-part-1-introducing-mqtt/",
            "https://www.hivemq.com/blog/how-to-get-started-with-mqtt/",
        ],
        "days": [
            (
                "MQTT parts 1–2",
                ["HiveMQ Essentials parts 1–2", "Notes: broker vs client"],
                "Diagram pub/sub with broker in middle",
            ),
            (
                "Parts 3–4",
                ["Essentials 3–4", "Connect / publish / subscribe"],
                "Can list connection lifecycle steps",
            ),
            (
                "Parts 5–6",
                ["Topics + QoS", "When QoS 0 vs 1 matters for lab"],
                "QoS choice note for retained config-like data",
            ),
            (
                "Parts 7–8 retained",
                [
                    "Persistent session + **Retained messages** (critical)",
                    "Map to README commission 'retained MQTT shadows'",
                ],
                "Explain retain in commission context",
            ),
            (
                "Parts 9–10",
                ["LWT + keep alive", "Skim MQTT getting-started tutorial"],
                "Essentials series complete",
            ),
            (
                "Saturday: hands-on MQTT",
                [
                    "Run local Mosquitto or HiveMQ Cloud free tier",
                    "Publish retained message; new subscriber sees it",
                    "Python client script pub/sub (paho-mqtt)",
                ],
                "Demo: retain visible to late subscriber",
            ),
            (
                "Sunday: commission mapping",
                [
                    "Read commissioning.py — MQTT/shadow steps (even if skim)",
                    "Write sequence diagram: SSH → write shadows → restart services → discovery",
                    "Coach review",
                ],
                "Sequence diagram exists",
            ),
        ],
    },
    {
        "title": "UART/serial + CAN/Modbus literacy",
        "phase": "4 — Protocols",
        "goal": "Log-scrape verification mindset; enough bus literacy to trust SBX evidence.",
        "resources": [
            "https://pyserial.readthedocs.io/",
            "https://www.csselectronics.com/pages/can-bus-simple-intro-tutorial",
            "https://www.simplymodbus.ca/FAQ.htm",
            "drive_automation/sbx_verify.py + sbx_decode.py",
        ],
        "days": [
            (
                "UART basics",
                [
                    "SparkFun/any UART primer + pySerial overview",
                    "README capture phase — local SBX/CAN/UART",
                ],
                "Know baud/framing; why 'no frames yet' can be OK pre-commission",
            ),
            (
                "pySerial practice",
                [
                    "Write a line-reader that works on a local pty or sample file if no hardware",
                ],
                "Script parses lines from a sample log file",
            ),
            (
                "CAN intro",
                [
                    "CSS Electronics CAN bus tutorial (core pages)",
                ],
                "Explain ID + payload at high level",
            ),
            (
                "Modbus intro",
                [
                    "Simply Modbus FAQ / skim protocol PDF",
                    "Req/ack thinking",
                ],
                "Map to 'AssignDynamicBusId + Ack' style verifies",
            ),
            (
                "Read sbx_verify",
                [
                    "Read sbx_verify.py + jama pair emission",
                    "List soft-verify message pairs",
                ],
                "Cheatsheet of pairs you verify",
            ),
            (
                "Saturday: log verifier",
                [
                    "scripts/practice/w10_log_verify.py: given sample log, mark seen/not-seen pairs",
                    "Emit JAMA-like JSON cases",
                    "Optional: skim sbx_decode.py",
                ],
                "Verifier outputs table + cases JSON",
            ),
            (
                "Sunday: Phase 4 gate",
                [
                    "End-to-end notes: MQTT retain + serial evidence + soft verify",
                    "Phase 4 gate; coach review",
                ],
                "Phase 4 gate passed",
            ),
        ],
    },
    {
        "title": "Flask operator UI + SSE",
        "phase": "5 — Productize",
        "goal": "Thin dashboard over an orchestrator with live phase events.",
        "resources": [
            "https://blog.miguelgrinberg.com/post/the-flask-mega-tutorial-part-i-hello-world",
            "https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events",
            "dashboard/app.py (read)",
        ],
        "days": [
            (
                "Flask Mega-Tutorial start",
                ["Mega-Tutorial parts on hello + templates/routing (core)"],
                "Local hello-world Flask runs",
            ),
            (
                "JSON APIs in Flask",
                ["Build POST /run and GET /status on practice app"],
                "API returns JSON status",
            ),
            (
                "SSE theory",
                ["MDN Server-Sent Events", "EventSource basics"],
                "Can explain SSE vs websocket for lab UI",
            ),
            (
                "Read dashboard/app.py",
                [
                    "Map routes: run/stop/events/artifacts",
                    "Note how orchestrator is invoked",
                ],
                "Route → responsibility table",
            ),
            (
                "UX rules",
                [
                    "README: decommission separate; sidebar phases",
                    "Write 5 UX rules for lab dashboards",
                ],
                "Rules written",
            ),
            (
                "Saturday: dashboard over toy orchestrator",
                [
                    "Wire w05/w06 orchestrator to Flask",
                    "SSE stream of JSONL events to a simple HTML page",
                    "Run + Stop buttons",
                ],
                "Browser shows live phases; Stop works",
            ),
            (
                "Sunday: artifacts endpoint + review",
                [
                    "Add download of last run zip/artifact",
                    "Separate /decommission stub that is NOT in Run",
                    "Coach review",
                ],
                "Decommission is separate route; Phase 5 checkpoint ok",
            ),
        ],
    },
    {
        "title": "pytest + hardening the practice stack",
        "phase": "6 — Testing & rigor",
        "goal": "Test clients and orchestrator pieces; treat evidence as a feature.",
        "resources": [
            "https://docs.pytest.org/",
            "https://realpython.com/pytest-python-testing/",
            "drive_automation/jama_cases.py",
        ],
        "days": [
            (
                "pytest fixtures",
                ["pytest docs: fixtures, tmp_path, monkeypatch"],
                "Rewrite one practice test using fixtures",
            ),
            (
                "Testing clients",
                [
                    "Monkeypatch requests for practice client",
                    "Real Python pytest article",
                ],
                "Client tests without real network",
            ),
            (
                "Testing orchestrator",
                [
                    "Test soft vs hard fail paths in toy orchestrator",
                ],
                "≥5 orchestrator tests green",
            ),
            (
                "Evidence quality",
                [
                    "Define 'Definition of Done' for a lab phase (events + artifacts)",
                ],
                "DoD checklist written",
            ),
            (
                "Read report/jama",
                ["Skim report.py + jama_cases.py"],
                "Know how cases get IDs/titles",
            ),
            (
                "Saturday: test suite day",
                [
                    "Bring practice stack to ≥20 tests",
                    "Add CI-less local command: pytest scripts/practice -q",
                    "Fix flaky waits",
                ],
                "20+ tests passing",
            ),
            (
                "Sunday: Phase 6 checkpoint",
                [
                    "Document how to run practice suite",
                    "Coach review; plan capstone",
                ],
                "Test doc + week review done",
            ),
        ],
    },
    {
        "title": "Capstone week A — design a mini lab pipeline",
        "phase": "Capstone",
        "goal": "Design and start building an end-to-end mini system with real structure.",
        "resources": [
            "All prior notes",
            "This repo as reference architecture (do not copy blindly — understand)",
        ],
        "days": [
            (
                "Pick capstone scope",
                [
                    "Choose: 'Fake EVSE throttle lab' OR contribute a real improvement design to Drive Automation",
                    "Write 1-page design: phases, soft/hard, artifacts, safety",
                ],
                "Design doc merged into notes/capstone_design.md",
            ),
            (
                "Skeleton repo/package",
                ["Create package layout + config env pattern + README"],
                "Skeleton runs --help",
            ),
            (
                "HTTP client module",
                ["Implement device client(s) against mocks"],
                "Client tests green",
            ),
            (
                "SSH/mock gateway module",
                ["Remote runner interface + fake implementation"],
                "Interface stable",
            ),
            (
                "Orchestrator skeleton",
                ["Phases + events + finally teardown"],
                "Empty phases emit events",
            ),
            (
                "Saturday: implement core phases",
                [
                    "Implement boot/work/verify/recover/teardown for real",
                    "Dynamic loop if throttle-themed",
                ],
                "Happy-path run works end-to-end on mocks",
            ),
            (
                "Sunday: mid-capstone review",
                [
                    "Record 10-min walkthrough notes",
                    "List gaps for week 14",
                    "Coach review",
                ],
                "Gap list ≤5 items",
            ),
        ],
    },
    {
        "title": "Capstone week B — ship + Drive Automation fluency",
        "phase": "Capstone",
        "goal": "Finish capstone; prove you can read and extend Drive Automation confidently.",
        "resources": [
            "drive_automation/orchestrator.py (targeted deep reads)",
            "Your capstone",
        ],
        "days": [
            (
                "Capstone: cancel + soft fail",
                ["Finish cancel, skips, soft verify"],
                "All failure modes demoable",
            ),
            (
                "Capstone: dashboard polish",
                ["SSE + Run/Stop + separate dangerous action"],
                "UI usable by a teammate",
            ),
            (
                "Capstone: artifacts + cases",
                ["Zip artifacts; emit case JSON"],
                "Artifact download works",
            ),
            (
                "Drive Automation deep dive",
                [
                    "Trace one full iteration charge→recover in orchestrator",
                    "Write annotated notes with line references",
                ],
                "Annotation note completed",
            ),
            (
                "Propose one real improvement",
                [
                    "Write a short RFC: one improvement to Drive Automation (clarity, safety, or test)",
                ],
                "RFC in notes/rfc_drive_automation.md",
            ),
            (
                "Saturday: ship capstone",
                [
                    "Polish README; record demo script",
                    "Final pytest run; fix blockers",
                    "Self-grade against Phase gates 0–6",
                ],
                "Capstone README + demo script exist; tests green",
            ),
            (
                "Sunday: graduation",
                [
                    "Fill graduation rubric in coach",
                    "Archive notes; set 30-day maintenance habit (1h/week on real repo)",
                    "Final coach review — PROGRAM COMPLETE",
                ],
                "Graduation rubric ≥ pass score",
            ),
        ],
    },
]


def _expand() -> dict:
    weeks_out: list[dict] = []
    for i, week in enumerate(WEEKS):
        start = START + timedelta(weeks=i)
        end = start + timedelta(days=6)
        sessions: list[dict] = []
        for d, (title, tasks, done_when) in enumerate(week["days"]):
            day = start + timedelta(days=d)
            hours = WEEKEND_H if day.weekday() >= 5 else WEEKDAY_H
            sessions.append(
                {
                    "id": f"w{i + 1:02d}-{day.strftime('%a').lower()}",
                    "date": day.isoformat(),
                    "dow": day.strftime("%a"),
                    "hours": hours,
                    "title": title,
                    "tasks": tasks,
                    "done_when": done_when,
                }
            )
        weeks_out.append(
            {
                "week": i + 1,
                "title": week["title"],
                "phase": week["phase"],
                "goal": week["goal"],
                "resources": week["resources"],
                "start": start.isoformat(),
                "end": end.isoformat(),
                "deadline": end.isoformat(),
                "hours_planned": sum(s["hours"] for s in sessions),
                "sessions": sessions,
            }
        )
    return {
        "program": {
            "name": "Lab Automation Pro Track",
            "tagline": "Build systems like Drive Automation — on purpose.",
            "start_date": START.isoformat(),
            "end_date": (START + timedelta(weeks=len(WEEKS) - 1, days=6)).isoformat(),
            "weeks": len(WEEKS),
            "hours_per_week": 5 * WEEKDAY_H + 2 * WEEKEND_H,
            "weekday_hours": WEEKDAY_H,
            "weekend_hours": WEEKEND_H,
            "timezone_note": "Dates are calendar days in your local timezone (IST).",
        },
        "push_rules": [
            "Study block is non-negotiable; move it earlier, don't delete it.",
            "Mark sessions done the same day via learning_coach.py done",
            "If you miss a weekday, catch up on the NEXT weekend (don't silently skip).",
            "Max debt: 3 sessions. If debt > 3, pause new material and burn down debt Saturday.",
            "Sunday last 30 min is always coach review — never skip review.",
            "Phase gates are hard deadlines: do not start next phase week until gate note exists.",
            "One practice script > three passive videos. Build every Saturday.",
        ],
        "phase_gates": [
            {
                "id": "gate-0",
                "after_week": 2,
                "name": "Foundations",
                "deadline": (START + timedelta(weeks=1, days=6)).isoformat(),
                "criteria": [
                    "module_map.md ≥ 10 modules",
                    "toy session client always logouts in finally",
                    "can find PIPELINE_PHASES and explain teardown",
                ],
            },
            {
                "id": "gate-1",
                "after_week": 4,
                "name": "HTTP clients",
                "deadline": (START + timedelta(weeks=3, days=6)).isoformat(),
                "criteria": [
                    "fake device + client with poll + cancel",
                    "wait_until tested",
                    "clear timeout policy written",
                ],
            },
            {
                "id": "gate-2",
                "after_week": 6,
                "name": "Orchestration",
                "deadline": (START + timedelta(weeks=5, days=6)).isoformat(),
                "criteria": [
                    "5-phase orchestrator with JSONL events",
                    "throttle simulator with recover",
                    "decommission-not-in-run design explained",
                ],
            },
            {
                "id": "gate-3",
                "after_week": 8,
                "name": "SSH / deploy",
                "deadline": (START + timedelta(weeks=7, days=6)).isoformat(),
                "criteria": [
                    "ssh_run wrapper with timeout",
                    "env renderer",
                    "cert vs password auth matrix",
                ],
            },
            {
                "id": "gate-4",
                "after_week": 10,
                "name": "Protocols",
                "deadline": (START + timedelta(weeks=9, days=6)).isoformat(),
                "criteria": [
                    "MQTT retain demo",
                    "log verifier with seen/not-seen cases",
                    "commission sequence diagram",
                ],
            },
            {
                "id": "gate-5",
                "after_week": 11,
                "name": "Product UI",
                "deadline": (START + timedelta(weeks=10, days=6)).isoformat(),
                "criteria": [
                    "Flask + SSE live phases",
                    "Stop works",
                    "dangerous action separate from Run",
                ],
            },
            {
                "id": "gate-6",
                "after_week": 12,
                "name": "Testing",
                "deadline": (START + timedelta(weeks=11, days=6)).isoformat(),
                "criteria": [
                    "≥20 practice tests green",
                    "DoD checklist for a phase",
                ],
            },
            {
                "id": "gate-grad",
                "after_week": 14,
                "name": "Graduation",
                "deadline": (START + timedelta(weeks=13, days=6)).isoformat(),
                "criteria": [
                    "capstone happy path + failure modes",
                    "Drive Automation annotation notes",
                    "RFC for one real improvement",
                    "graduation rubric pass",
                ],
            },
        ],
        "weeks": weeks_out,
    }


def main() -> None:
    out = Path(__file__).resolve().parent / "curriculum.json"
    data = _expand()
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    prog = data["program"]
    print(
        f"Wrote {out.name}: {prog['weeks']} weeks, "
        f"{prog['start_date']} → {prog['end_date']}, "
        f"{prog['hours_per_week']} h/week"
    )


if __name__ == "__main__":
    main()
