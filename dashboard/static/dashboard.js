const $ = (id) => document.getElementById(id);

/** Abort hung API calls so one SSH/HTTP stall cannot freeze every button. */
function fetchWithTimeout(url, opts = {}, ms = 15000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  return fetch(url, { ...opts, signal: ctrl.signal }).finally(() => clearTimeout(timer));
}

const CLIENT_KEY = "drive_automation_client_id";
const VIEW_KEY = "drive_dash_view";
const MAX_POINTS = 360;

const PANEL_MAP = {
  pipeline: true,
  jama: true,
  live: true,
  safety: true,
  gateway: true,
  decommission: true,
  capture: true,
  logs: true,
};

let lastAvailability = null;
let pendingFormData = null;
let evtSource = null;
let clientId = localStorage.getItem(CLIENT_KEY) || "";
let role = "claiming";
let dutyChart = null;
let cpChart = null;
let panelChart = null;
let plotT0 = null;
let panelCapacityA = 125;
let driveMaxA = 48;
let evMaxA = 48;
const gauges = {};
const eventLogLines = [];
const pipelineActivityLines = [];

let phaseState = {
  name: "—",
  iteration: null,
  startedAtMs: null,
  durationSec: null,
};

const DEFAULT_PIPELINE_PHASES = [
  "capture", "commission", "post_commission_verify", "boot", "handshake",
  "panel_ratings", "charge", "aggregate", "throttle", "verify", "recover",
];
const DEFAULT_JAMA_PHASES = [
  "capture", "boot", "handshake", "snapshot", "jama_test", "restore",
];
/** Map fine-grained orchestrator phase names → stepper pipeline phase. */
const PHASE_TO_PIPELINE = {
  login: "boot",
  startup: "boot",
  test_new: "boot",
  test_save: "boot",
  ev_export: "boot",
  artifacts: "boot",
  capture_started: "capture",
  uart_capture_started: "capture",
  commission: "commission",
  pilot_watch: "handshake",
  pilot_connect: "handshake",
  pilot_wait_pwm: "handshake",
  pilot_mode: "handshake",
  pilot_charge: "charge",
  ev_switch_loads_on: "charge",
  lb2_delay: "charge",
  ev_switch_loads_off: "charge",
  pilot_shutdown: "charge",
  lb2_voltages: "charge",
  aggregate: "aggregate",
  throttle: "throttle",
  verify: "verify",
  recover: "recover",
  recovery: "recover",
  lb2_ramp_up: "aggregate",
  lb2_ramp_down: "recover",
  safe_exit: "recover",
  handshake: "handshake",
  handshake_verify: "handshake",
  jama_snapshot: "snapshot",
  jama_test: "jama_test",
  jama_restore: "restore",
};
const validationChecks = [];
let pipelinePhases = DEFAULT_PIPELINE_PHASES.slice();
let activePipelinePhase = "";
let failedPipelinePhase = "";
let activeRunId = "";
let pipelineRunning = false;
/** "throttle" | "jama" — which one-click path is active / was last started. */
let activePipelineMode = "throttle";
let knownJamaCases = [];
let lastSetpointAmps = null;
let lastSetpointSource = "";
let lastChargeRateA = null;
const EV_DRAW_FRACTION = 0.8;

function setDashView(view) {
  const key = view && PANEL_MAP[view] ? view : "pipeline";
  document.body.dataset.dashView = key;
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.view === key);
  });
  try {
    sessionStorage.setItem(VIEW_KEY, key);
  } catch (_err) {
    /* ignore */
  }
  if (key === "live") {
    requestAnimationFrame(() => {
      try {
        dutyChart?.resize();
        cpChart?.resize();
        panelChart?.resize();
      } catch (_e) {
        /* ignore */
      }
    });
  }
}

function bindNav() {
  document.querySelectorAll(".nav-item[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => setDashView(btn.dataset.view));
  });
  let restored = "pipeline";
  try {
    restored = sessionStorage.getItem(VIEW_KEY) || "pipeline";
  } catch (_err) {
    restored = "pipeline";
  }
  setDashView(restored);
}

function setNavBadge(id, text, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = `nav-badge${cls ? ` ${cls}` : ""}`;
}

function setStatusCardChrome(status) {
  const card = $("pipe-status-card");
  if (!card) return;
  card.classList.remove("running", "success", "failed");
  const st = status || "idle";
  if (st === "running" || st === "stopping") card.classList.add("running");
  else if (st === "complete" || st === "success") card.classList.add("success");
  else if (st === "error" || st === "failed") card.classList.add("failed");
}

function setPipeHasJob(hasJob) {
  const card = $("pipe-status-card");
  if (card) card.classList.toggle("has-job", !!hasJob);
  document.body.classList.toggle("pipe-has-job", !!hasJob);
}

function setPipeLiveStatus(status, message) {
  const title = $("pipe-live-title");
  const pill = $("pipe-live-pill");
  if (title && message) title.textContent = message;
  if (pill) {
    pill.textContent = status || "idle";
    pill.className = `gw-phase-pill ${status || "idle"}`;
  }
  setStatusCardChrome(status);
  const ov = $("ov-pipeline");
  if (ov) ov.textContent = activePipelinePhase || status || "idle";
  const badgeId = activePipelineMode === "jama" ? "nav-badge-jama" : "nav-badge-pipeline";
  const otherBadge = activePipelineMode === "jama" ? "nav-badge-pipeline" : "nav-badge-jama";
  if (status === "running") setNavBadge(badgeId, "running", "running");
  else if (status === "complete") setNavBadge(badgeId, "done", "done");
  else if (status === "error") setNavBadge(badgeId, "failed", "failed");
  else if (status === "stopping") setNavBadge(badgeId, "stopping", "running");
  else setNavBadge(badgeId, "idle", "");
  if (status === "idle" || !status) setNavBadge(otherBadge, "idle", "");
}

function activityLevel(record) {
  const ev = String(record.event || "");
  if (ev === "error" || ev === "run_failed" || ev.endsWith("_error") || record.ok === false) {
    return "error";
  }
  if (ev === "gateway_cmd" && Number(record.exit) !== 0) {
    return "warn";
  }
  if (
    ev.includes("timeout")
    || ev.includes("skipped")
    || ev === "pilot_retry"
    || ev === "stop_requested"
    || ev === "aggregation_ceiling"
    || (ev === "validation_check" && record.ok === false)
  ) {
    return "warn";
  }
  if (ev === "validation_check" && record.ok === true) return "ok";
  return "info";
}

function formatActivityMessage(record) {
  if (record.event === "gateway_cmd") {
    const cmd = String(record.command || "").trim();
    const code = record.exit != null ? record.exit : "";
    const out = String(record.output || "").trim();
    return out
      ? `$ ${cmd}  (exit ${code})  ${out}`
      : `$ ${cmd}  (exit ${code})`;
  }
  const base = String(record.message || record.event || "");
  const bits = [base];
  if (record.event === "validation_check" && record.check) {
    bits[0] = `[${record.check}] ${base}`;
  }
  if (record.expected != null) bits.push(`expected: ${fmtVal(record.expected)}`);
  if (record.actual != null) bits.push(`actual: ${fmtVal(record.actual)}`);
  if (record.error && record.error !== record.message) bits.push(String(record.error));
  return bits.join(" · ");
}

function fmtVal(v) {
  if (v == null) return "—";
  if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") return String(v);
  try { return JSON.stringify(v); } catch (_) { return String(v); }
}

function renderValidationChecklist() {
  const el = $("validation-checklist");
  if (!el) return;
  if (!validationChecks.length) {
    el.innerHTML = '<div class="validation-empty">Checks appear here as the run progresses.</div>';
    return;
  }
  el.innerHTML = validationChecks.map((row) => {
    const cls = row.ok ? "pass" : "fail";
    const icon = row.ok ? "ti-circle-check" : "ti-circle-x";
    const meta = [];
    if (row.expected != null) meta.push(`expected ${escapeHtml(fmtVal(row.expected))}`);
    if (row.actual != null) meta.push(`got ${escapeHtml(fmtVal(row.actual))}`);
    return (
      `<div class="validation-row ${cls}" role="listitem">`
      + `<i class="ti ${icon}" aria-hidden="true"></i>`
      + `<div class="validation-body">`
      + `<div class="validation-title"><span class="validation-check">${escapeHtml(row.check)}</span>`
      + `<span class="validation-t">${escapeHtml(row.t)}</span></div>`
      + `<div class="validation-msg">${escapeHtml(row.message)}</div>`
      + (meta.length ? `<div class="validation-meta">${meta.join(" · ")}</div>` : "")
      + `</div></div>`
    );
  }).join("");
  el.scrollTop = el.scrollHeight;
}

function upsertValidationCheck(record) {
  const check = String(record.check || record.event || "check");
  const row = {
    check,
    ok: record.ok !== false,
    message: String(record.message || ""),
    expected: record.expected,
    actual: record.actual,
    t: (record.ts || "").slice(11, 19) || "--:--:--",
  };
  const idx = validationChecks.findIndex((r) => r.check === check);
  if (idx >= 0) validationChecks[idx] = row;
  else validationChecks.push(row);
  if (validationChecks.length > 40) validationChecks.splice(0, validationChecks.length - 40);
  renderValidationChecklist();
}

function renderPipelineActivity() {
  const html = pipelineActivityLines.map((row) => (
    `<div class="gw-activity-line ${row.level}">`
    + `<span class="t">${row.t}</span>`
    + `<span class="m">${escapeHtml(row.msg)}</span>`
    + `</div>`
  )).join("");
  const el = $("pipeline-activity");
  if (el) {
    el.innerHTML = html;
    el.scrollTop = el.scrollHeight;
  }
  const jamaEl = $("jama-activity");
  if (jamaEl && activePipelineMode === "jama") {
    jamaEl.innerHTML = html;
    jamaEl.scrollTop = jamaEl.scrollHeight;
  }
}

function hideFailureCard() {
  const card = $("pipe-failure-card");
  if (card) card.classList.add("hidden");
  const detail = $("fail-detail");
  if (detail) {
    detail.textContent = "";
    detail.classList.add("hidden");
  }
}

function showFailureCard(record) {
  setPipeHasJob(true);
  const card = $("pipe-failure-card");
  if (!card) return;
  card.classList.remove("hidden");
  const pipe = record.pipeline_phase || activePipelinePhase || "—";
  const phase = record.phase || phaseState.name || "—";
  const iter = record.iteration != null ? record.iteration : (phaseState.iteration ?? "—");
  if ($("fail-pipeline-phase")) $("fail-pipeline-phase").textContent = String(pipe);
  if ($("fail-phase")) $("fail-phase").textContent = String(phase);
  if ($("fail-iteration")) $("fail-iteration").textContent = String(iter);
  const sp = record.charge_rate_a != null
    ? `${fmtNum(record.charge_rate_a, 1)} A (sbx)`
    : (lastSetpointAmps != null
      ? `${fmtNum(lastSetpointAmps, 1)} A (${lastSetpointSource || "—"})`
      : "—");
  if ($("fail-setpoint")) $("fail-setpoint").textContent = sp;
  if ($("fail-message")) {
    $("fail-message").textContent = record.message || record.error || "Run failed";
  }
  const detail = $("fail-detail");
  if (detail) {
    const bits = [];
    if (record.error && record.error !== record.message) bits.push(String(record.error));
    if (record.detail) {
      bits.push(typeof record.detail === "string"
        ? record.detail
        : JSON.stringify(record.detail, null, 2));
    }
    if (bits.length) {
      detail.textContent = bits.join("\n");
      detail.classList.remove("hidden");
    } else {
      detail.textContent = "";
      detail.classList.add("hidden");
    }
  }
  const phaseEl = $("phase-name");
  if (phaseEl) {
    phaseEl.textContent = String(phase);
    phaseEl.className = "err";
  }
}

function clearPipeReportLinks() {
  const box = $("pipe-report-links");
  if (box) box.classList.add("hidden");
  [
    "pipe-report-link",
    "pipe-modbus-link",
    "pipe-pilot-link",
    "pipe-ev-loads-link",
    "pipe-lb2-link",
    "pipe-panel-link",
    "pipe-zip-link",
  ].forEach((id) => {
    const a = $(id);
    if (a) {
      a.href = "#";
      a.classList.add("hidden");
      a.removeAttribute("download");
    }
  });
}

function showPipeReportLinks({
  reportUrl,
  modbusLogUrl,
  pilotLogUrl,
  evLoadsUrl,
  lb2LogUrl,
  panelLogUrl,
  downloadUrl,
  combinedName,
}) {
  const box = $("pipe-report-links");
  if (!box) return;
  let any = false;

  const pairs = [
    ["pipe-report-link", reportUrl],
    ["pipe-modbus-link", modbusLogUrl],
    ["pipe-pilot-link", pilotLogUrl],
    ["pipe-ev-loads-link", evLoadsUrl],
    ["pipe-lb2-link", lb2LogUrl],
    ["pipe-panel-link", panelLogUrl],
  ];
  pairs.forEach(([id, url]) => {
    const a = $(id);
    if (!a) return;
    if (url) {
      a.href = url;
      a.classList.remove("hidden");
      any = true;
    } else {
      a.classList.add("hidden");
    }
  });

  const z = $("pipe-zip-link");
  if (z && downloadUrl) {
    z.href = downloadUrl;
    z.textContent = combinedName ? `Download ${combinedName}` : "Download full zip";
    if (combinedName) z.setAttribute("download", combinedName);
    z.classList.remove("hidden");
    any = true;
  } else if (z) {
    z.classList.add("hidden");
  }

  box.classList.toggle("hidden", !any);
}

function resetPipelineUi(runId, mode = "throttle") {
  // Empty activeRunId accepts events until the server run_id is bound.
  activeRunId = runId || "";
  activePipelineMode = mode === "jama" ? "jama" : "throttle";
  failedPipelinePhase = "";
  activePipelinePhase = "";
  pipelinePhases = (
    activePipelineMode === "jama" ? DEFAULT_JAMA_PHASES : DEFAULT_PIPELINE_PHASES
  ).slice();
  pipelineActivityLines.length = 0;
  validationChecks.length = 0;
  lastSetpointAmps = null;
  lastSetpointSource = "";
  lastChargeRateA = null;
  phaseState = {
    name: "—",
    iteration: null,
    startedAtMs: null,
    durationSec: null,
  };
  hideFailureCard();
  clearPipeReportLinks();
  renderValidationChecklist();
  renderPipelineActivity();
  renderPipelineStepper("");
  const art = $("artifact-box");
  if (art) art.classList.add("hidden");
  const artReport = $("artifact-report");
  if (artReport) {
    artReport.href = "#";
    artReport.classList.add("hidden");
  }
  setPipeHasJob(true);
  renderPipelineStepper("");
  renderPipelineActivity();
  updatePhaseTimerUi();
  updateSetpoint(null, null);
  ["phase-name", "phase-iter", "phase-remaining", "phase-duration"].forEach((id) => {
    const el = $(id);
    if (el) {
      el.textContent = id === "phase-elapsed" ? "00:00" : "—";
      if (id === "phase-name") el.className = "accent";
    }
  });
  if ($("phase-elapsed")) $("phase-elapsed").textContent = "00:00";
  if ($("phase-progress")) $("phase-progress").style.width = "0%";
  if (gauges.duty) gauges.duty.set(NaN);
  if (gauges.drive) gauges.drive.set(NaN);
  if (gauges.ev) gauges.ev.set(NaN);
  if (gauges.panel) gauges.panel.set(NaN);
  const ovPanel = $("ov-panel");
  if (ovPanel) ovPanel.textContent = "—";
  const ovPilot = $("ov-pilot");
  if (ovPilot) ovPilot.textContent = "—";
  const ovDuty = $("ov-duty");
  if (ovDuty) ovDuty.textContent = "—";
  const ovLb2 = $("ov-lb2");
  if (ovLb2) ovLb2.textContent = "—";
  resetPlots();
}

function ensureClientId() {
  if (!clientId) {
    clientId = (crypto.randomUUID && crypto.randomUUID())
      || `c_${Date.now()}_${Math.random().toString(16).slice(2)}`;
    localStorage.setItem(CLIENT_KEY, clientId);
  }
  return clientId;
}

function setRunStatus(status, message) {
  const el = $("run-status");
  el.className = `run-status ${status}`;
  el.textContent = message || status;
  setPipeLiveStatus(status, message || status);
}

function appendPipelineActivity(record) {
  if (!record || record.event === "telemetry") return;
  const msg = formatActivityMessage(record);
  pipelineActivityLines.push({
    t: (record.ts || "").slice(11, 19) || "--:--:--",
    msg: String(msg),
    level: activityLevel(record),
  });
  if (pipelineActivityLines.length > 400) {
    pipelineActivityLines.splice(0, pipelineActivityLines.length - 400);
  }
  renderPipelineActivity();
}

function appendLog(record) {
  const line = JSON.stringify(record);
  eventLogLines.push(line);
  if (eventLogLines.length > 5000) {
    eventLogLines.splice(0, eventLogLines.length - 5000);
  }
  const log = $("event-log");
  if (log) {
    log.textContent = eventLogLines.join("\n") + "\n";
    log.scrollTop = log.scrollHeight;
  }
  appendPipelineActivity(record);
}

function clearEventLog() {
  eventLogLines.length = 0;
  $("event-log").textContent = "";
}

function saveEventLog() {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const name = `drive_automation_events_${stamp}.log`;
  const body = eventLogLines.length
    ? `${eventLogLines.join("\n")}\n`
    : "";
  const blob = new Blob([body], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function fmtNum(v, digits = 1) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

function fmtClock(sec) {
  if (sec == null || !Number.isFinite(sec)) return "—";
  const s = Math.max(0, Math.floor(sec));
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
}

function renderPipelineStepper(active) {
  const activeName = (active || activePipelinePhase || "").toLowerCase();
  const failName = (failedPipelinePhase || "").toLowerCase();
  const idx = pipelinePhases.indexOf(activeName);
  const failIdx = pipelinePhases.indexOf(failName);
  const html = pipelinePhases.map((name, i) => {
    const key = String(name).toLowerCase();
    let cls = "pipe-step";
    if (failName && key === failName) cls += " failed";
    else if (failIdx >= 0 && i < failIdx) cls += " done";
    else if (!failName && idx >= 0 && i < idx) cls += " done";
    else if (!failName && key === activeName && activeName) cls += " active";
    return `<span class="${cls}" data-phase="${key}">${key}</span>`;
  }).join("");
  const el = $("pipeline-stepper");
  if (el) el.innerHTML = html;
  const jamaEl = $("jama-stepper");
  if (jamaEl && activePipelineMode === "jama") jamaEl.innerHTML = html;
}

function setActivePipelinePhase(name, { render = true } = {}) {
  const raw = String(name || "").trim();
  if (!raw) return;
  const mapped = PHASE_TO_PIPELINE[raw] || PHASE_TO_PIPELINE[raw.toLowerCase()] || raw.toLowerCase();
  const known =
    pipelinePhases.includes(mapped)
    || DEFAULT_PIPELINE_PHASES.includes(mapped)
    || DEFAULT_JAMA_PHASES.includes(mapped);
  if (!known) {
    // Unknown fine-grained phase — leave stepper alone.
    return;
  }
  if (activePipelinePhase === mapped) {
    return;
  }
  activePipelinePhase = mapped;
  if (render) renderPipelineStepper(mapped);
  const ov = $("ov-pipeline");
  if (ov) ov.textContent = activePipelinePhase || "—";
}

function updateDutyGauge(dutyPct) {
  if (dutyPct == null || dutyPct === "") return;
  const n = Number(dutyPct);
  if (!Number.isFinite(n)) return;
  if (gauges.duty) gauges.duty.set(n);
  const ovDuty = $("ov-duty");
  if (ovDuty) {
    const adv = ovDuty.textContent.includes("/")
      ? ovDuty.textContent.split("/")[1]
      : "";
    ovDuty.textContent = adv
      ? `${fmtNum(n, 1)}% /${adv}`
      : `${fmtNum(n, 1)}%`;
  }
}

function updateSetpoint(amps, source) {
  lastSetpointAmps = amps == null ? null : Number(amps);
  lastSetpointSource = source || "";
  const aEl = $("setpoint-amps");
  const sEl = $("setpoint-source");
  if (aEl) aEl.textContent = amps == null ? "—" : `${fmtNum(amps, 1)} A`;
  if (sEl) {
    sEl.textContent = source || "—";
    sEl.className = `setpoint-badge source-${source || "none"}`;
  }
  const ov = $("ov-setpoint");
  if (ov) {
    ov.textContent = amps == null
      ? "—"
      : `${fmtNum(amps, 1)} A (${source || "—"})`;
  }
  if (gauges.drive) gauges.drive.set(amps == null ? NaN : Number(amps));
}

function applyRole(payload) {
  role = payload.role || "viewer";
  const badge = $("role-badge");
  badge.className = `role-badge ${role}`;
  badge.textContent = role === "controller" ? "Controller" : "Viewer";

  const banner = $("viewer-banner");
  const isViewer = role !== "controller";
  document.body.classList.toggle("viewer-mode", isViewer);
  if (isViewer) {
    banner.classList.remove("hidden");
    banner.textContent = payload.viewer_reason
      || "Viewer only — another dashboard client holds control.";
  } else {
    banner.classList.add("hidden");
    banner.textContent = "";
  }

  const running = !!payload.running;
  if (!isViewer) {
    if ($("start-btn")) $("start-btn").disabled = running;
    if ($("stop-btn")) $("stop-btn").disabled = !running;
    if ($("jama-start-btn")) $("jama-start-btn").disabled = running;
    if ($("jama-stop-btn")) $("jama-stop-btn").disabled = !running;
  }
  if (Array.isArray(payload.jama_cases) && payload.jama_cases.length) {
    knownJamaCases = payload.jama_cases;
    populateJamaCaseList();
  }
}

function populateJamaCaseList() {
  const box = $("jama-case-list");
  if (!box || !knownJamaCases.length) return;
  if (box.dataset.ready === "1") return;
  box.innerHTML = knownJamaCases.map((c) => {
    const id = String(c.id || "");
    const label = String(c.label || id);
    return (
      `<label>`
      + `<input type="checkbox" name="jama_case" value="${id}" checked>`
      + `<span><code>${id}</code> — ${label}</span>`
      + `</label>`
    );
  }).join("");
  box.dataset.ready = "1";
}

async function syncRole(action = "heartbeat") {
  ensureClientId();
  const res = await fetch("/api/role", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action,
      client_id: clientId,
      label: "Dashboard",
    }),
  });
  const data = await res.json();
  applyRole(data);
  // Thread alive is source of truth — do not trust a stale run_status alone.
  const running = !!data.running;
  const prevRunning = pipelineRunning;
  pipelineRunning = running;

  // Keep stepper in sync even when SSE drops pipeline_phase events.
  const pipe =
    data.run_status?.pipeline_phase
    || data.pipeline_phase
    || null;
  if (running && pipe && pipe !== "idle") {
    setActivePipelinePhase(pipe);
  }

  // Reconcile sidebar/live status with server (fixes stuck "running" badge).
  const rs = data.run_status || {};
  const rsStatus = String(rs.status || "");
  if (running) {
    if (!prevRunning || rsStatus === "running" || rsStatus === "stopping") {
      setPipeLiveStatus(
        rsStatus === "stopping" ? "stopping" : "running",
        rs.message || (rsStatus === "stopping" ? "Stopping…" : "Test running")
      );
      setPipeHasJob(true);
    }
  } else if (prevRunning || document.getElementById("nav-badge-pipeline")?.textContent === "running") {
    if (rsStatus === "complete" || rsStatus === "error" || rsStatus === "failed") {
      setPipeLiveStatus(
        rsStatus === "complete" ? "complete" : "error",
        rs.message || rsStatus
      );
      setPipeHasJob(true);
    } else {
      setPipeLiveStatus("idle", "No run active");
      setPipeHasJob(false);
      if (role === "controller") {
        const startBtn = $("start-btn");
        const stopBtn = $("stop-btn");
        if (startBtn) startBtn.disabled = false;
        if (stopBtn) stopBtn.disabled = true;
        if ($("jama-start-btn")) $("jama-start-btn").disabled = false;
        if ($("jama-stop-btn")) $("jama-stop-btn").disabled = true;
      }
    }
  }

  const pending =
    running
      ? (data.ceiling_override_pending
        || data.run_status?.ceiling_override_pending
        || null)
      : null;
  if (pending && typeof pending === "object") {
    const modal = $("ceiling-modal");
    if (modal && modal.classList.contains("hidden")) {
      showCeilingOverrideModal({ event: "ceiling_override_prompt", ...pending });
      setPipeLiveStatus("running", "Waiting for LB2 ceiling decision…");
    }
  } else if (!running) {
    hideCeilingOverrideModal();
  }
  return data;
}

function updatePhaseTimerUi() {
  $("phase-name").textContent = phaseState.name || "—";
  $("phase-iter").textContent = String(phaseState.iteration ?? "—");

  let elapsed = null;
  if (phaseState.startedAtMs != null) {
    elapsed = (Date.now() - phaseState.startedAtMs) / 1000;
  }
  $("phase-elapsed").textContent = fmtClock(elapsed);

  const dur = phaseState.durationSec;
  $("phase-duration").textContent = dur != null ? fmtClock(dur) : "—";

  let remaining = null;
  let pct = 0;
  if (dur != null && elapsed != null) {
    remaining = Math.max(0, dur - elapsed);
    pct = Math.min(100, (elapsed / Math.max(dur, 0.001)) * 100);
  }
  $("phase-remaining").textContent = remaining != null ? fmtClock(remaining) : "—";
  $("phase-progress").style.width = `${pct}%`;
}

function setPhaseFromEvent(record) {
  phaseState.name = record.phase || record.message || "—";
  if (record.iteration != null) phaseState.iteration = record.iteration;
  if (record.started_at) {
    const t = Date.parse(record.started_at);
    phaseState.startedAtMs = Number.isFinite(t) ? t : Date.now();
  } else {
    phaseState.startedAtMs = Date.now();
  }
  phaseState.durationSec = record.duration_sec != null
    ? Number(record.duration_sec)
    : (record.phase_duration_sec != null ? Number(record.phase_duration_sec) : null);
  updatePhaseTimerUi();
}

function renderEv(ev) {
  if (!ev) return;
  const mode = (ev.pilot_mode || "—").toString().toUpperCase();
  const duty = fmtNum(ev.duty_cycle_pct, 1);
  const freq = fmtNum(ev.frequency_hz, 0);
  const cph = fmtNum(ev.cp_high_v, 2);
  const cpl = fmtNum(ev.cp_low_v, 2);
  const adv = fmtNum(ev.adv_current_a, 1);
  const chg = fmtNum(ev.applied_current_a ?? ev.target_current_a, 1);
  const maxA = fmtNum(ev.max_current_a, 0);

  $("oled-state").textContent = mode;
  $("oled-hv").textContent = `${cph} V`;
  $("oled-duty").textContent = `${duty} %`;
  $("oled-freq").textContent = `${freq} Hz`;
  $("oled-adva").textContent = `${adv} A`;
  $("oled-chga").textContent = `${chg} A`;
  $("oled-mode").textContent = mode;

  $("pilot-state").textContent = mode;
  $("pilot-current").textContent = `${adv} A`;
  $("pilot-charging").textContent = `${chg} A`;
  $("pilot-maxset").textContent = `${maxA} A`;
  $("pilot-cph").textContent = `${cph} V`;
  $("pilot-cpl").textContent = `${cpl} V`;

  const ovPilot = $("ov-pilot");
  if (ovPilot) ovPilot.textContent = mode;
  const ovDuty = $("ov-duty");
  if (ovDuty) ovDuty.textContent = `${duty}% / ${adv}A`;

  if (gauges.duty) gauges.duty.set(Number(ev.duty_cycle_pct));
  if (gauges.ev) gauges.ev.set(Number(ev.applied_current_a ?? ev.target_current_a));
}

function parseLoadKey(key) {
  // e.g. "3:5A" or "ch3_5A"
  const m = String(key).match(/(\d+)\D+(\d+(?:\.\d+)?A)/i);
  if (m) return { ch: m[1], load: m[2].toUpperCase() };
  const parts = String(key).split(/[:_]/);
  if (parts.length >= 2) return { ch: parts[0].replace(/\D/g, ""), load: parts.slice(1).join("_") };
  return { ch: "?", load: String(key) };
}

function renderLb2(lb2) {
  const box = $("lb2-channels");
  if (!lb2) {
    box.textContent = "—";
    $("lb2-total-w").textContent = "0 W";
    $("lb2-total-a").textContent = "0.0 A";
    return;
  }

  const volts = lb2.voltages || {};
  const meters = lb2.meters || {};
  const loads = lb2.loads || {};
  const byCh = {};

  Object.keys(volts).forEach((ch) => {
    byCh[ch] = byCh[ch] || { loads: [] };
    byCh[ch].volt = volts[ch];
  });
  Object.keys(meters).forEach((ch) => {
    byCh[ch] = byCh[ch] || { loads: [] };
    byCh[ch].P = meters[ch]?.P;
    byCh[ch].I = meters[ch]?.I;
  });
  Object.keys(loads).forEach((key) => {
    if (!loads[key]) return;
    const { ch, load } = parseLoadKey(key);
    byCh[ch] = byCh[ch] || { loads: [] };
    byCh[ch].loads.push(load);
  });

  const channels = Object.keys(byCh).sort((a, b) => Number(a) - Number(b));
  if (!channels.length) {
    box.textContent = "No channel data";
  } else {
    box.innerHTML = channels.map((ch) => {
      const d = byCh[ch];
      const loadHtml = (d.loads || []).length
        ? d.loads.map((l) => `<span class="load-chip">${l}</span>`).join("")
        : `<span class="load-chip off">OFF</span>`;
      return `
        <div class="lb2-ch">
          <div class="lb2-ch-title"><span>CH${ch}</span><span>${d.volt ?? "—"}</span></div>
          <div class="lb2-ch-metrics">
            <span>${fmtNum(d.P, 0)} W</span>
            <span>${fmtNum(d.I, 2)} A</span>
          </div>
          <div class="lb2-loads">${loadHtml}</div>
        </div>`;
    }).join("");
  }

  const tw = lb2.total_watts != null
    ? lb2.total_watts
    : Object.values(meters).reduce((s, m) => s + (Number(m?.P) || 0), 0);
  const ta = lb2.total_amps != null
    ? lb2.total_amps
    : Object.values(meters).reduce((s, m) => s + (Number(m?.I) || 0), 0);
  $("lb2-total-w").textContent = `${fmtNum(tw, 0)} W`;
  $("lb2-total-a").textContent = `${fmtNum(ta, 2)} A`;
  const ovLb2 = $("ov-lb2");
  if (ovLb2) ovLb2.textContent = `${fmtNum(ta, 1)} A · ${fmtNum(tw, 0)} W`;
}

function chartDefaults() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    scales: {
      x: {
        type: "linear",
        title: { display: true, text: "Time (s)", color: "#8b9cb3" },
        ticks: { color: "#8b9cb3", maxTicksLimit: 6 },
        grid: { color: "#1f2937" },
      },
    },
    plugins: {
      legend: { display: false },
    },
  };
}

function initCharts() {
  if (typeof Chart === "undefined") {
    console.warn("Chart.js missing — live plots disabled (UI still works)");
    return;
  }
  const dutyCanvas = $("duty-current-chart");
  const cpCanvas = $("cp-volt-chart");
  if (!dutyCanvas || !cpCanvas) return;
  const dutyCtx = dutyCanvas.getContext("2d");
  const cpCtx = cpCanvas.getContext("2d");

  dutyChart = new Chart(dutyCtx, {
    type: "line",
    data: {
      datasets: [
        {
          label: "Duty %",
          data: [],
          borderColor: "#38bdf8",
          backgroundColor: "transparent",
          yAxisID: "yDuty",
          pointRadius: 0,
          borderWidth: 2,
          tension: 0.15,
        },
        {
          label: "Current A",
          data: [],
          borderColor: "#f59e0b",
          backgroundColor: "transparent",
          yAxisID: "yCurr",
          pointRadius: 0,
          borderWidth: 2,
          tension: 0.15,
        },
      ],
    },
    options: {
      ...chartDefaults(),
      scales: {
        ...chartDefaults().scales,
        yDuty: {
          position: "left",
          title: { display: true, text: "Duty %", color: "#38bdf8" },
          ticks: { color: "#38bdf8" },
          grid: { color: "#1f2937" },
          suggestedMin: 0,
          suggestedMax: 100,
        },
        yCurr: {
          position: "right",
          title: { display: true, text: "Current A", color: "#f59e0b" },
          ticks: { color: "#f59e0b" },
          grid: { drawOnChartArea: false },
          suggestedMin: 0,
        },
      },
    },
  });

  cpChart = new Chart(cpCtx, {
    type: "line",
    data: {
      datasets: [
        {
          label: "CPH",
          data: [],
          borderColor: "#22c55e",
          backgroundColor: "transparent",
          pointRadius: 0,
          borderWidth: 2,
          tension: 0.15,
        },
        {
          label: "CPL",
          data: [],
          borderColor: "#a78bfa",
          backgroundColor: "transparent",
          pointRadius: 0,
          borderWidth: 2,
          tension: 0.15,
        },
      ],
    },
    options: {
      ...chartDefaults(),
      scales: {
        ...chartDefaults().scales,
        y: {
          title: { display: true, text: "Volts", color: "#8b9cb3" },
          ticks: { color: "#8b9cb3" },
          grid: { color: "#1f2937" },
        },
      },
    },
  });

  const panelEl = $("panel-current-chart");
  if (panelEl) {
    panelChart = new Chart(panelEl.getContext("2d"), {
      type: "line",
      data: {
        datasets: [
          {
            label: "Panel A",
            data: [],
            borderColor: "#a78bfa",
            backgroundColor: "rgba(167,139,250,0.12)",
            fill: true,
            pointRadius: 0,
            borderWidth: 2,
            tension: 0.15,
          },
          {
            label: "Capacity",
            data: [],
            borderColor: "rgba(248,81,73,0.6)",
            backgroundColor: "transparent",
            borderDash: [6, 4],
            pointRadius: 0,
            borderWidth: 1.5,
          },
        ],
      },
      options: {
        ...chartDefaults(),
        scales: {
          ...chartDefaults().scales,
          y: {
            title: { display: true, text: "Panel current (A)", color: "#8b9cb3" },
            ticks: { color: "#8b9cb3" },
            grid: { color: "#1f2937" },
            suggestedMin: 0,
          },
        },
      },
    });
    if (panelEl.parentElement) panelEl.parentElement.style.height = "220px";
  }

  if (dutyCanvas.parentElement) dutyCanvas.parentElement.style.height = "220px";
  if (cpCanvas.parentElement) cpCanvas.parentElement.style.height = "220px";
}

function pushPoint(dataset, x, y) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) return;
  dataset.data.push({ x, y });
  if (dataset.data.length > MAX_POINTS) dataset.data.shift();
}

function updatePlots(ev) {
  if (!ev || !dutyChart || !cpChart) return;
  if (plotT0 == null) plotT0 = Date.now();
  const t = (Date.now() - plotT0) / 1000;

  const duty = Number(ev.duty_cycle_pct);
  const curr = Number(ev.applied_current_a ?? ev.target_current_a);
  const cph = Number(ev.cp_high_v);
  const cpl = Number(ev.cp_low_v);

  pushPoint(dutyChart.data.datasets[0], t, duty);
  pushPoint(dutyChart.data.datasets[1], t, curr);
  pushPoint(cpChart.data.datasets[0], t, cph);
  pushPoint(cpChart.data.datasets[1], t, cpl);
  dutyChart.update("none");
  cpChart.update("none");
}

function resetPlots() {
  plotT0 = Date.now();
  [dutyChart, cpChart, panelChart].forEach((c) => {
    if (!c) return;
    c.data.datasets.forEach((d) => { d.data = []; });
    c.update("none");
  });
}

function updatePanelPlot(total) {
  if (!panelChart) return;
  if (plotT0 == null) plotT0 = Date.now();
  const t = (Date.now() - plotT0) / 1000;
  pushPoint(panelChart.data.datasets[0], t, total);
  pushPoint(panelChart.data.datasets[1], t, panelCapacityA);
  panelChart.update("none");
}

/* ── SVG gauges (speedometer style) ─────────────────────────────────────── */
const GAUGE_START = -135;
const GAUGE_SWEEP = 270;

function gaugePolar(cx, cy, r, angleDeg) {
  const a = (angleDeg - 90) * Math.PI / 180;
  return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
}

function gaugeArc(cx, cy, r, a0, a1) {
  const p0 = gaugePolar(cx, cy, r, a0);
  const p1 = gaugePolar(cx, cy, r, a1);
  const large = (a1 - a0) > 180 ? 1 : 0;
  return `M ${p0.x.toFixed(2)} ${p0.y.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${p1.x.toFixed(2)} ${p1.y.toFixed(2)}`;
}

function panelGaugeColor(v, max) {
  if (!Number.isFinite(v) || !(max > 0)) return "#8b5cf6";
  const f = v / max;
  if (f >= 0.9) return "#f85149";
  if (f >= 0.7) return "#d29922";
  return "#3fb950";
}

function createGauge(el, opts) {
  if (!el) return null;
  const o = Object.assign(
    { min: 0, max: 100, unit: "", color: "#8b5cf6", decimals: 1 },
    opts || {},
  );
  const cx = 100, cy = 100, r = 82, sw = 14;
  const track = gaugeArc(cx, cy, r, GAUGE_START, GAUGE_START + GAUGE_SWEEP);
  el.innerHTML = `
    <svg viewBox="0 0 200 176" class="gauge-svg" preserveAspectRatio="xMidYMid meet">
      <path class="gauge-track" d="${track}" fill="none" stroke-width="${sw}" stroke-linecap="round"></path>
      <path class="gauge-value" d="" fill="none" stroke-width="${sw}" stroke-linecap="round"></path>
      <circle class="gauge-knob" r="6.5" cx="0" cy="0" style="display:none"></circle>
      <text class="gauge-num" x="100" y="104" text-anchor="middle">—</text>
      <text class="gauge-unit" x="100" y="128" text-anchor="middle">${o.unit}</text>
    </svg>`;
  const valPath = el.querySelector(".gauge-value");
  const knob = el.querySelector(".gauge-knob");
  const num = el.querySelector(".gauge-num");
  const state = { ...o, value: NaN };

  function render() {
    const max = state.max > state.min ? state.max : state.min + 1;
    const finite = Number.isFinite(Number(state.value));
    const v = finite ? Math.min(Math.max(Number(state.value), state.min), max) : state.min;
    const frac = (v - state.min) / (max - state.min);
    const a1 = GAUGE_START + frac * GAUGE_SWEEP;
    valPath.setAttribute("d", gaugeArc(cx, cy, r, GAUGE_START, finite ? a1 : GAUGE_START));
    const color = typeof state.color === "function"
      ? state.color(finite ? Number(state.value) : NaN, max)
      : state.color;
    valPath.setAttribute("stroke", color);
    const kp = gaugePolar(cx, cy, r, a1);
    knob.setAttribute("cx", kp.x.toFixed(2));
    knob.setAttribute("cy", kp.y.toFixed(2));
    knob.setAttribute("stroke", color);
    knob.style.display = finite ? "" : "none";
    num.textContent = finite ? Number(state.value).toFixed(state.decimals) : "—";
  }

  render();
  return {
    set(value, over) {
      if (over && over.max != null) state.max = Number(over.max);
      state.value = value;
      render();
    },
    setMax(max) {
      if (Number.isFinite(Number(max)) && Number(max) > 0) {
        state.max = Number(max);
        render();
      }
    },
  };
}

function initGauges() {
  gauges.panel = createGauge($("gauge-panel"), { max: panelCapacityA, unit: "A", color: panelGaugeColor, decimals: 1 });
  gauges.duty = createGauge($("gauge-duty"), { max: 100, unit: "%", color: "#38bdf8", decimals: 0 });
  gauges.drive = createGauge($("gauge-drive"), { max: driveMaxA, unit: "A", color: "#f59e0b", decimals: 0 });
  gauges.ev = createGauge($("gauge-ev"), { max: evMaxA, unit: "A", color: "#22c55e", decimals: 1 });
}

function setPanelCapacity(cap) {
  if (!Number.isFinite(Number(cap)) || Number(cap) <= 0) return;
  panelCapacityA = Number(cap);
  if (gauges.panel) gauges.panel.setMax(panelCapacityA);
  const sub = $("gauge-panel-sub");
  if (sub) sub.textContent = `of ${fmtNum(panelCapacityA, 0)} A capacity`;
}

function syncEvDrawDisplay() {
  const brIn = document.querySelector('[name="breaker_rating_a"]');
  const br = Number(brIn && brIn.value);
  const draw = Number.isFinite(br) && br > 0 ? Math.round(EV_DRAW_FRACTION * br) : null;
  const val = $("ev-draw-value");
  const summary = $("ev-draw-summary");
  if (val) val.textContent = draw == null ? "— A" : `${draw} A`;
  if (summary) {
    summary.textContent = draw == null ? "80% of rating" : `${draw} A (80%)`;
  }
  if (draw != null) {
    evMaxA = draw;
    if (gauges.ev) gauges.ev.setMax(draw);
  }
}

function syncGaugeMaxes() {
  const brIn = document.querySelector('[name="breaker_rating_a"]');
  const br = Number(brIn && brIn.value);
  if (Number.isFinite(br) && br > 0) {
    driveMaxA = br;
    if (gauges.drive) gauges.drive.setMax(br);
    const sub = $("gauge-drive-sub");
    if (sub) sub.textContent = `max ${fmtNum(br, 0)} A`;
  }
  syncEvDrawDisplay();
}

function syncCommissionRequired() {
  const on = !!($("run_commission") && $("run_commission").checked);
  document.querySelectorAll(".commission-req").forEach((el) => {
    el.classList.toggle("hidden", !on);
  });
  ["top_position", "bottom_position", "drive_serial"].forEach((name) => {
    const input = document.querySelector(`[name="${name}"]`);
    if (!input) return;
    if (on) input.setAttribute("aria-required", "true");
    else input.removeAttribute("aria-required");
  });
}

function syncLegacyRampVisibility() {
  // Step interval / step size are always visible (Dynamic and open-loop).
}

function syncLb2ChannelVoltageControls() {
  document.querySelectorAll(".lb2-ch-enable").forEach((box) => {
    const ch = box.getAttribute("data-ch");
    const sel = document.querySelector(`.lb2-ch-voltage[data-ch="${ch}"]`);
    if (!sel) return;
    sel.disabled = !box.checked;
  });
}

function initLb2ChannelSelection() {
  document.querySelectorAll(".lb2-ch-enable").forEach((box) => {
    box.addEventListener("change", syncLb2ChannelVoltageControls);
  });
  syncLb2ChannelVoltageControls();
}

function showCeilingOverrideModal(record) {
  const modal = $("ceiling-modal");
  if (!modal) return;
  const duty = record.duty_cycle_pct;
  const ceiling = record.max_lb2_amps;
  const atHw = !!record.at_hardware_max;
  const suggested = record.suggested_ceiling_a != null
    ? Number(record.suggested_ceiling_a)
    : Number(ceiling || 0) + 20;
  const title = $("ceiling-modal-title");
  const text = $("ceiling-modal-text");
  const hint = $("ceiling-modal-hint");
  const input = $("ceiling-modal-new-a");
  const cont = $("ceiling-modal-continue");
  if (title) {
    title.textContent = atHw
      ? "LB2 at hardware max — continue?"
      : "Raise LB2 ceiling?";
  }
  if (text) {
    if (atHw) {
      text.textContent =
        `LB2 is at hardware max ~${record.hardware_max_amps != null ? record.hardware_max_amps : ceiling} A` +
        (record.achieved_amps != null ? ` (physical ~${record.achieved_amps} A)` : "") +
        `, but the Drive/CP is not fully throttled` +
        (duty != null ? ` (duty ${Number(duty).toFixed(1)}% > 10%).` : " (duty unavailable).") +
        " Raising the UI ceiling cannot add more amps. Continue to sustain/verify, or stop aggregation.";
    } else {
      text.textContent =
        `LB2 reached ${ceiling} A` +
        (record.achieved_amps != null ? ` (physical ~${record.achieved_amps} A)` : "") +
        (record.hardware_max_amps != null ? `, hardware max ~${record.hardware_max_amps} A` : "") +
        `, but the Drive/CP is not fully throttled` +
        (duty != null ? ` (duty ${Number(duty).toFixed(1)}% > 10%).` : " (duty unavailable).") +
        " Raise the LB2 ceiling to keep stepping, or stop aggregation.";
    }
  }
  if (hint) {
    hint.textContent =
      `Charge rate: ${record.charge_rate_a != null ? record.charge_rate_a + " A" : "—"} · ` +
      `Commanded: ${record.commanded_amps != null ? record.commanded_amps + " A" : "—"}` +
      (record.meter_amps != null ? ` · Meter: ${record.meter_amps} A` : "");
  }
  if (input) {
    input.value = String(suggested);
    input.min = String(Number(ceiling || 0) + (atHw ? 0 : 0.5));
    input.disabled = atHw;
    input.style.opacity = atHw ? "0.5" : "";
  }
  if (cont) {
    cont.textContent = atHw ? "Continue to sustain" : "Continue past ceiling";
  }
  modal.classList.remove("hidden");
}

function hideCeilingOverrideModal() {
  const modal = $("ceiling-modal");
  if (modal) modal.classList.add("hidden");
}

async function submitCeilingOverride(allow) {
  if (role !== "controller") {
    appendLog({ event: "error", message: "Viewer only — cannot answer ceiling override" });
    return;
  }
  const input = $("ceiling-modal-new-a");
  const body = {
    client_id: clientId,
    allow: !!allow,
  };
  if (allow) {
    body.new_ceiling_a = Number((input && input.value) || 0);
  }
  hideCeilingOverrideModal();
  const res = await fetch("/api/run/ceiling-override", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  appendLog({
    event: allow ? "ceiling_override_submit" : "ceiling_override_decline_click",
    ...data,
    message: data.message || (allow ? "Ceiling raise submitted" : "Ceiling raise declined"),
  });
  if (!data.ok) {
    appendLog({ event: "error", message: data.error || "Ceiling override failed" });
    // Do not re-open the modal on stale/late clicks after the wait ended.
    hideCeilingOverrideModal();
  }
}

function initCeilingOverrideModal() {
  const cont = $("ceiling-modal-continue");
  const stop = $("ceiling-modal-stop");
  if (cont) cont.addEventListener("click", () => submitCeilingOverride(true));
  if (stop) stop.addEventListener("click", () => submitCeilingOverride(false));
}

function closeAllFieldHelp(exceptId) {
  document.querySelectorAll(".field-help-panel").forEach((panel) => {
    if (exceptId && panel.id === exceptId) return;
    panel.hidden = true;
  });
  document.querySelectorAll(".field-help").forEach((btn) => {
    if (exceptId && btn.getAttribute("aria-controls") === exceptId) return;
    btn.setAttribute("aria-expanded", "false");
  });
}

function initFieldHelp() {
  document.querySelectorAll(".field-help[data-help]").forEach((btn) => {
    const key = btn.dataset.help;
    const panel = $(`help-${key}`);
    if (!panel) return;
    btn.setAttribute("aria-controls", panel.id);
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const open = panel.hidden;
      closeAllFieldHelp(open ? panel.id : null);
      panel.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open) {
        const grp = btn.closest("details.config-group");
        if (grp && !grp.open) grp.open = true;
      }
    });
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest(".field-help") || e.target.closest(".field-help-panel")) return;
    closeAllFieldHelp();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeAllFieldHelp();
  });
}

function initConfigGroups() {
  document.querySelectorAll(".config-summary-val[data-summary-for]").forEach((span) => {
    const name = span.dataset.summaryFor;
    const input = document.querySelector(`[name="${name}"]`);
    if (!input) return;
    const suffix = span.dataset.summarySuffix || "";
    const empty = span.dataset.summaryEmpty || "—";
    const sync = () => {
      const v = (input.value || "").trim();
      span.textContent = v ? `${v}${suffix}` : empty;
    };
    input.addEventListener("input", sync);
    input.addEventListener("change", sync);
    sync();
  });
  document.querySelectorAll("#test-form [required]").forEach((input) => {
    input.addEventListener("invalid", () => {
      const grp = input.closest("details.config-group");
      if (grp && !grp.open) grp.open = true;
    });
  });
  const br = document.querySelector('[name="breaker_rating_a"]');
  if (br) br.addEventListener("input", syncGaugeMaxes);
  const rc = $("run_commission");
  if (rc) rc.addEventListener("change", syncCommissionRequired);
  const dyn = $("use_dynamic_load");
  if (dyn) dyn.addEventListener("change", syncLegacyRampVisibility);
  syncGaugeMaxes();
  syncCommissionRequired();
  syncLegacyRampVisibility();
  initFieldHelp();
  initCeilingOverrideModal();
  initLb2ChannelSelection();
}

function setSseBanner(show) {
  const el = $("sse-banner");
  if (!el) return;
  el.classList.toggle("hidden", !show);
}

function connectEvents() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource("/api/events");
  evtSource.onopen = () => setSseBanner(false);
  evtSource.onmessage = (msg) => {
    setSseBanner(false);
    try {
      const record = JSON.parse(msg.data);
      handleEvent(record);
    } catch (_) { /* keepalive */ }
  };
  evtSource.onerror = () => {
    setSseBanner(true);
    // Native EventSource already reconnects. Only recreate after a hard close
    // so we do not tear down a live stream (that left the UI on "reconnecting").
    if (evtSource && evtSource.readyState === EventSource.CLOSED) {
      setTimeout(connectEvents, 3000);
    }
  };
}

function showArtifactReady(record) {
  const box = $("artifact-box");
  const link = $("artifact-download");
  const text = $("artifact-text");
  const reportUrl = record.report_url || (record.run_id ? `/api/artifacts/${record.run_id}/report` : "");
  const modbusLogUrl =
    record.modbus_log_url ||
    (record.run_id ? `/api/artifacts/${record.run_id}/modbus` : "");
  const pilotLogUrl =
    record.pilot_log_url ||
    (record.run_id ? `/api/artifacts/${record.run_id}/pilot` : "");
  const evLoadsUrl =
    record.ev_loads_log_url ||
    (record.run_id ? `/api/artifacts/${record.run_id}/ev_loads` : "");
  const lb2LogUrl =
    record.lb2_log_url ||
    (record.run_id ? `/api/artifacts/${record.run_id}/lb2` : "");
  const panelLogUrl =
    record.panel_log_url ||
    (record.run_id ? `/api/artifacts/${record.run_id}/panel` : "");
  const url = record.download_url;
  const name = record.combined_name || "drive_run.zip";
  if (!reportUrl && !modbusLogUrl && !pilotLogUrl && !evLoadsUrl && !lb2LogUrl && !panelLogUrl && !url) return;

  setPipeHasJob(true);
  if (box) box.classList.remove("hidden");
  if (text) {
    const abn = record.sbx_abnormality_count != null ? Number(record.sbx_abnormality_count) : null;
    text.textContent = [
      `Run: ${record.run_id || "—"}`,
      reportUrl ? "HTML report: available" : "",
      modbusLogUrl ? "Modbus / SBX log: available" : "",
      pilotLogUrl ? "Pilot log: available" : "",
      evLoadsUrl ? "EV loads log: available" : "",
      lb2LogUrl ? "LB2 log: available" : "",
      panelLogUrl ? "Panel current log: available" : "",
      url ? "Full zip: available" : "",
      abn != null && abn > 0 ? `SBX abnormalities: ${abn} (see report)` : "",
    ].filter(Boolean).join("\n");
  }

  const linkPairs = [
    ["artifact-report", reportUrl],
    ["artifact-modbus", modbusLogUrl],
    ["artifact-pilot", pilotLogUrl],
    ["artifact-ev-loads", evLoadsUrl],
    ["artifact-lb2", lb2LogUrl],
    ["artifact-panel", panelLogUrl],
  ];
  linkPairs.forEach(([id, href]) => {
    const a = $(id);
    if (!a) return;
    if (href) {
      a.href = href;
      a.classList.remove("hidden");
    } else {
      a.classList.add("hidden");
    }
  });
  if (link) {
    if (url) {
      link.href = url;
      link.setAttribute("download", name);
      link.textContent = `Download ${name}`;
      link.classList.remove("hidden");
    } else {
      link.classList.add("hidden");
    }
  }
  showPipeReportLinks({
    reportUrl,
    modbusLogUrl,
    pilotLogUrl,
    evLoadsUrl,
    lb2LogUrl,
    panelLogUrl,
    downloadUrl: url,
    combinedName: name,
  });
}

function handleEvent(record) {
  if (record.run_id && activeRunId && record.run_id !== activeRunId) {
    // Ignore late events from a previous run.
    if (!["controller_claimed", "controller_released"].includes(record.event)) {
      return;
    }
  }

  if (record.event === "telemetry") {
    setPipeHasJob(true);
    renderEv(record.ev);
    renderLb2(record.lb2);
    updatePlots(record.ev);
    if (record.phase) {
      phaseState.name = record.phase;
      setActivePipelinePhase(record.phase);
      if (record.iteration != null) phaseState.iteration = record.iteration;
      if (record.phase_duration_sec != null) {
        phaseState.durationSec = Number(record.phase_duration_sec);
      }
      if (record.phase_elapsed_sec != null && phaseState.startedAtMs == null) {
        phaseState.startedAtMs = Date.now() - Number(record.phase_elapsed_sec) * 1000;
      }
      updatePhaseTimerUi();
    }
    return;
  }

  appendLog(record);

  if (record.event === "phase") {
    setPipeHasJob(true);
    setPhaseFromEvent(record);
    setActivePipelinePhase(record.phase);
    const phaseEl = $("phase-name");
    if (phaseEl && !failedPipelinePhase) phaseEl.className = "accent";
  }
  if (record.event === "pipeline_phase") {
    setPipeHasJob(true);
    if (Array.isArray(record.steps) && record.steps.length) {
      pipelinePhases = record.steps.map((s) => String(s).toLowerCase());
      if (pipelinePhases.includes("jama_test") || pipelinePhases.includes("snapshot")) {
        activePipelineMode = "jama";
      } else if (pipelinePhases.includes("throttle") || pipelinePhases.includes("aggregate")) {
        activePipelineMode = "throttle";
      }
    }
    setActivePipelinePhase(record.phase || "");
  }
  if (record.event === "validation_check") {
    setPipeHasJob(true);
    upsertValidationCheck(record);
    if (record.ok === false) {
      setPipeLiveStatus("error", record.message || `Check failed: ${record.check || ""}`);
    }
  }
  if (record.event === "load_setpoint") {
    updateSetpoint(record.setpoint_amps, record.source);
  }
  // Drive setpoint: update as soon as any event carries charge_rate_a (not only
  // mid-aggregation). Late UI was caused by waiting for aggregation_sample.
  if (record.charge_rate_a != null && record.charge_rate_a !== "") {
    lastChargeRateA = Number(record.charge_rate_a);
    const src =
      record.event === "load_setpoint"
        ? (record.source || "sbx")
        : (record.charge_rate_source || "sbx");
    updateSetpoint(lastChargeRateA, src);
  }
  // Duty gauge: prefer live aggregation samples over waiting for telemetry.
  if (record.duty_cycle_pct != null && record.duty_cycle_pct !== "") {
    updateDutyGauge(record.duty_cycle_pct);
  }
  if (record.event === "aggregation_sample" && record.charge_rate_a != null) {
    updateSetpoint(record.charge_rate_a, "sbx");
  }
  if (record.event === "throttle_engaged") {
    setPipeLiveStatus("running", "Throttling started");
    setNavBadge("nav-badge-pipeline", "throttling", "running");
    if (record.charge_rate_a != null) updateSetpoint(record.charge_rate_a, "sbx");
  }
  if (record.event === "ceiling_override_prompt") {
    // Late SSE reminders must not reopen the modal after the run finished.
    if (!pipelineRunning) {
      hideCeilingOverrideModal();
      return;
    }
    setPipeHasJob(true);
    setPipeLiveStatus("running", "Waiting for LB2 ceiling decision…");
    try {
      showCeilingOverrideModal(record);
    } catch (err) {
      appendLog({ event: "error", message: `Ceiling modal failed: ${err}` });
    }
  }
  if (record.event === "ceiling_override_accepted") {
    hideCeilingOverrideModal();
    setPipeLiveStatus("running", record.message || "LB2 ceiling raised — continuing");
  }
  if (
    record.event === "ceiling_override_declined"
    || record.event === "ceiling_override_timeout"
    || record.event === "ceiling_override_cancelled"
    || record.event === "ceiling_override_cleared"
    || record.event === "aggregation_hardware_saturated"
  ) {
    hideCeilingOverrideModal();
  }
  if (record.event === "dashboard_status") {
    if (record.status === "complete" || record.status === "error") {
      pipelineRunning = false;
      hideCeilingOverrideModal();
    }
    if (record.status === "complete") {
      setPipeHasJob(true);
      setRunStatus("complete", "Complete");
      if (role === "controller") {
        if ($("start-btn")) $("start-btn").disabled = false;
        if ($("stop-btn")) $("stop-btn").disabled = true;
        if ($("jama-start-btn")) $("jama-start-btn").disabled = false;
        if ($("jama-stop-btn")) $("jama-stop-btn").disabled = true;
      }
    }
  }
  if (record.event === "run_failed" || record.event === "artifacts_ready") {
    if (record.event === "run_failed") {
      pipelineRunning = false;
    }
    hideCeilingOverrideModal();
  }
  if (record.event === "throttle_step_6a") {
    setPipeLiveStatus("running", "Throttled to 6 A");
    if (record.charge_rate_a != null) updateSetpoint(record.charge_rate_a, "sbx");
  }
  if (record.event === "throttle_zero") {
    setPipeLiveStatus("running", "Throttled to 0 A");
    if (record.charge_rate_a != null) updateSetpoint(record.charge_rate_a, "sbx");
  }
  if (record.event === "recovery_step" && record.charge_rate_a != null) {
    updateSetpoint(record.charge_rate_a, "sbx");
  }
  if (record.event === "capture_started") {
    setPipeLiveStatus("running", "Capture started (SBX+CAN+UART)");
  }
  if (record.event === "panel_capacity") {
    setPipeLiveStatus("running", `Panel capacity ${fmtNum(record.site_utility_rating_a, 0)} A`);
    setPanelCapacity(record.site_utility_rating_a);
  }
  if (record.event === "panel_ratings" || record.event === "panel_limit_meter") {
    const lim = record.limit_a != null ? record.limit_a : record.site_utility_rating_a;
    if (lim != null) setPanelCapacity(lim);
    setPipeLiveStatus(
      "running",
      `Panel limit ${fmtNum(lim, 0)} A (80%=${fmtNum(record.throttle_threshold_a, 0)} A)`
    );
  }
  if (record.event === "feeder_amps" && record.combined_a != null) {
    if (gauges.panel) gauges.panel.set(Number(record.combined_a), { max: panelCapacityA });
    updatePanelPlot(Number(record.combined_a));
  }
  if (record.event === "throttle_engaged") {
    setPipeLiveStatus("running", record.message || "Throttling started");
  }
  if (record.event === "gateway_ready") {
    setPipeLiveStatus("running", record.message || "Gateway SSH OK");
  }
  if (record.event === "decommission_finished") {
    setRunStatus(record.ok ? "complete" : "error", record.message || "Decommission finished");
  }
  if (record.event === "sbx_abnormality") {
    setPipeLiveStatus("running", record.message || "SBX abnormality");
  }
  if (record.event === "artifact_ready" || record.event === "report_ready") {
    showArtifactReady(record);
  }
  if (record.event === "run_failed" || record.event === "error") {
    setPipeHasJob(true);
    pipelineRunning = false;
    hideCeilingOverrideModal();
    failedPipelinePhase = record.pipeline_phase || activePipelinePhase || failedPipelinePhase;
    if (record.phase) phaseState.name = record.phase;
    if (record.iteration != null) phaseState.iteration = record.iteration;
    if (record.charge_rate_a == null && lastChargeRateA != null) {
      record.charge_rate_a = lastChargeRateA;
    }
    showFailureCard(record);
    renderPipelineStepper(activePipelinePhase);
    setRunStatus("error", record.message || record.error || "Error");
    if (role === "controller") {
      if ($("start-btn")) $("start-btn").disabled = false;
      if ($("stop-btn")) $("stop-btn").disabled = true;
      if ($("jama-start-btn")) $("jama-start-btn").disabled = false;
      if ($("jama-stop-btn")) $("jama-stop-btn").disabled = true;
    }
  }
  if (record.event === "run_complete") {
    setPipeHasJob(true);
    pipelineRunning = false;
    hideCeilingOverrideModal();
    setRunStatus("complete", "Complete");
    if (role === "controller") {
      if ($("start-btn")) $("start-btn").disabled = false;
      if ($("stop-btn")) $("stop-btn").disabled = true;
      if ($("jama-start-btn")) $("jama-start-btn").disabled = false;
      if ($("jama-stop-btn")) $("jama-stop-btn").disabled = true;
    }
  }
  if (record.event === "stop_requested") {
    setRunStatus("stopping", record.message || "Stopping…");
  }
  if (record.event === "safe_shutdown_started") {
    setRunStatus("stopping", record.message || "Safe shutdown…");
  }
  if (record.event === "safe_shutdown_complete") {
    setRunStatus(record.ok ? "complete" : "error", record.message || "Safe shutdown finished");
    fetchAvailability();
    return;
  }
  if (record.event === "controller_claimed" || record.event === "controller_released") {
    syncRole("heartbeat");
  }
  if (
    record.message
    && ![
      "stop_requested", "safe_shutdown_started", "error", "run_failed",
      "run_complete", "phase",
    ].includes(record.event)
  ) {
    const el = $("run-status");
    if (!el.classList.contains("stopping") && !el.classList.contains("complete") && !el.classList.contains("error")) {
      setRunStatus("running", record.message);
    }
  }
}

async function fetchAvailability() {
  const box = $("availability-box");
  try {
    const res = await fetchWithTimeout("/api/availability", {}, 12000);
    const data = await res.json();
    lastAvailability = data;
    if (box) box.classList.remove("hidden", "ok", "warn", "err");

    if (!data.ok) {
      if (box) {
        box.classList.add("err");
        box.textContent = `Error: ${data.error}`;
      }
      return data;
    }

    const evOk = data.ev_available;
    const lb2Ok = data.lb2_available;
    if (!box) return data;
    if (evOk && lb2Ok) {
      box.classList.add("ok");
      box.textContent = "EV Simulator and Load Bank-2 are available.";
    } else if (evOk && !lb2Ok) {
      box.classList.add("warn");
      box.textContent = `EV Simulator: available\nLoad Bank-2: in use by ${data.lb2_operator || "another user"}`;
    } else if (!evOk) {
      box.classList.add("err");
      box.textContent = `EV Simulator: in use by ${data.ev?.operator || "another user"}`;
    }
    return data;
  } catch (err) {
    const data = {
      ok: false,
      error: err && err.name === "AbortError"
        ? "Availability check timed out (EV/LB2 HTTP)."
        : String(err.message || err),
    };
    lastAvailability = data;
    if (box) {
      box.classList.remove("hidden", "ok", "warn");
      box.classList.add("err");
      box.textContent = `Error: ${data.error}`;
    }
    return data;
  }
}

function formToObject(form) {
  const fd = new FormData(form);
  const obj = {};
  fd.forEach((v, k) => { obj[k] = v; });
  // Explicit booleans for checkboxes (unchecked fields are absent from FormData)
  obj.run_commission = !!(form.querySelector("[name=run_commission]") || {}).checked;
  obj.use_dynamic_load = !!(form.querySelector("[name=use_dynamic_load]") || {}).checked;
  // Position-based commissioning + global phase delay
  obj.top_position = (form.querySelector("[name=top_position]") || {}).value || "";
  obj.bottom_position = (form.querySelector("[name=bottom_position]") || {}).value || "";
  obj.phase_delay = (form.querySelector("[name=phase_delay]") || {}).value || "0";
  // LB2 channel enable flags (must be explicit — unchecked boxes are omitted from FormData)
  const channels = [];
  [1, 2, 3, 4].forEach((ch) => {
    const box = form.querySelector(`[name=lb2_ch${ch}]`);
    const on = !!(box && box.checked);
    obj[`lb2_ch${ch}`] = on;
    if (on) channels.push(ch);
  });
  obj.lb2_channels = channels;
  // Disabled <select>s are omitted from FormData — copy the on-screen value.
  [1, 2, 3, 4].forEach((ch) => {
    const sel = form.querySelector(`[name=lb2_v_ch${ch}]`);
    const fallback = ch <= 2 ? "120" : "240";
    obj[`lb2_v_ch${ch}`] = (sel && sel.value) ? String(sel.value) : fallback;
  });
  return obj;
}

async function startTest(useLb2) {
  if (!pendingFormData) return;
  if (role !== "controller") {
    appendLog({ event: "error", message: "Viewer only — cannot start test" });
    return;
  }
  if (useLb2) {
    const chans = pendingFormData.lb2_channels || [];
    if (!chans.length) {
      appendLog({ event: "error", message: "Select at least one LB2 load channel (CH1–CH4)." });
      setRunStatus("error", "LB2 channel required");
      return;
    }
  }
  if (pendingFormData.run_commission) {
    if (!(pendingFormData.top_position || "").toString().trim()
      || !(pendingFormData.bottom_position || "").toString().trim()) {
      appendLog({ event: "error", message: "Breaker top and bottom positions are required for commissioning." });
      setRunStatus("error", "Commission positions required");
      return;
    }
    if (!(pendingFormData.drive_serial || "").toString().trim()) {
      appendLog({ event: "error", message: "Drive serial is required when commissioning is enabled." });
      setRunStatus("error", "Drive serial required");
      return;
    }
  }
  const body = {
    ...pendingFormData,
    use_lb2: useLb2,
    pipeline_mode: "throttle",
    client_id: clientId,
  };
  $("start-btn").disabled = true;
  $("stop-btn").disabled = false;
  if ($("jama-start-btn")) $("jama-start-btn").disabled = true;
  if ($("jama-stop-btn")) $("jama-stop-btn").disabled = false;
  resetPipelineUi("", "throttle");
  pipelineRunning = true;
  setRunStatus("running", useLb2 ? "Starting full test…" : "Starting EV-only test…");

  const res = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!data.ok) {
    pipelineRunning = false;
    hideCeilingOverrideModal();
    failedPipelinePhase = "boot";
    showFailureCard({
      event: "run_failed",
      pipeline_phase: "boot",
      phase: "start",
      message: data.error || "Start failed",
      error: data.error || "Start failed",
    });
    renderPipelineStepper("boot");
    setRunStatus("error", data.error || "Start failed");
    $("start-btn").disabled = false;
    $("stop-btn").disabled = true;
    appendLog({ event: "error", message: data.error });
    return;
  }
  activeRunId = data.run_id || activeRunId || "";
  appendLog({
    event: "run_started",
    use_lb2: useLb2,
    run_id: activeRunId,
    message: data.message,
  });
  pendingFormData = null;
}

async function startJamaTest() {
  if (role !== "controller") {
    appendLog({ event: "error", message: "Viewer only — cannot start JAMA test" });
    return;
  }
  const form = $("jama-form");
  if (!form) return;
  const fd = new FormData(form);
  const selected = [];
  form.querySelectorAll("input[name=jama_case]:checked").forEach((box) => {
    selected.push(box.value);
  });
  if (!selected.length) {
    appendLog({ event: "error", message: "Select at least one JAMA case." });
    setRunStatus("error", "No JAMA cases selected");
    return;
  }
  const body = {
    pipeline_mode: "jama",
    use_lb2: false,
    test_name: String(fd.get("test_name") || "JamaTest01"),
    notes: String(fd.get("notes") || ""),
    breaker_instance_id: String(fd.get("breaker_instance_id") || "").trim(),
    jama_cases: selected,
    client_id: clientId,
  };

  const avail = await fetchAvailability();
  if (!avail.ok) return;
  if (!avail.ev_available) {
    showEvModal(avail.ev?.operator);
    return;
  }

  if ($("jama-start-btn")) $("jama-start-btn").disabled = true;
  if ($("jama-stop-btn")) $("jama-stop-btn").disabled = false;
  if ($("start-btn")) $("start-btn").disabled = true;
  if ($("stop-btn")) $("stop-btn").disabled = false;
  resetPipelineUi("", "jama");
  pipelineRunning = true;
  setRunStatus("running", "Starting JAMA test…");

  const res = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!data.ok) {
    pipelineRunning = false;
    failedPipelinePhase = "boot";
    renderPipelineStepper("boot");
    setRunStatus("error", data.error || "JAMA start failed");
    if ($("jama-start-btn")) $("jama-start-btn").disabled = false;
    if ($("jama-stop-btn")) $("jama-stop-btn").disabled = true;
    if ($("start-btn")) $("start-btn").disabled = false;
    if ($("stop-btn")) $("stop-btn").disabled = true;
    appendLog({ event: "error", message: data.error });
    return;
  }
  activeRunId = data.run_id || activeRunId || "";
  appendLog({
    event: "run_started",
    pipeline_mode: "jama",
    use_lb2: false,
    run_id: activeRunId,
    jama_cases: selected,
    message: data.message,
  });
}

function showEvModal(operator) {
  $("ev-modal-text").textContent =
    `EV Simulator is in use by ${operator || "another user"}. Cannot start test.`;
  $("ev-modal").classList.remove("hidden");
}

$("check-btn")?.addEventListener("click", () => fetchAvailability());

$("gateway-test-btn")?.addEventListener("click", async () => {
  const box = $("gateway-box");
  const btn = $("gateway-test-btn");
  box.classList.remove("hidden", "ok", "warn", "err");
  box.textContent = "Testing gateway SSH…";
  if (btn) btn.disabled = true;
  try {
    const res = await fetchWithTimeout("/api/gateway/test", { method: "POST" }, 20000);
    const data = await res.json();
    box.classList.add(data.ok ? "ok" : "err");
    box.textContent = data.message || JSON.stringify(data);
    appendLog({ event: "gateway_test", ...data });
  } catch (err) {
    box.classList.add("err");
    const timedOut = err && err.name === "AbortError";
    box.textContent = timedOut
      ? "Gateway SSH test timed out (20s). Check GATEWAY_SSH_HOST on this wall and: sudo journalctl -u drive-dashboard -n 40"
      : `Gateway SSH test failed: ${err.message || err}`;
    appendLog({ event: "gateway_test", ok: false, message: box.textContent });
  } finally {
    if (btn) btn.disabled = false;
  }
});

$("decommission-btn")?.addEventListener("click", async () => {
  if (role !== "controller") {
    appendLog({ event: "error", message: "Viewer only — cannot decommission" });
    return;
  }
  if (!window.confirm(
    "Run SEPARATE decommission?\n\n"
    + "This is NOT part of the one-click pipeline.\n"
    + "It stops eve, deletes the DER breaker, and clears MQTT shadows."
  )) {
    return;
  }
  const body = {
    client_id: clientId,
    confirm: true,
    breaker_instance_id: $("decom-breaker-id")?.value || "",
    top_bc_instance: $("decom-top-bc")?.value || "",
    bottom_bc_instance: $("decom-bottom-bc")?.value || "",
  };
  setRunStatus("running", "Decommission started…");
  const res = await fetch("/api/decommission", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  appendLog({ event: "decommission_request", ...data });
  if (!data.ok) {
    setRunStatus("error", data.error || "Decommission rejected");
  }
});

$("test-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (role !== "controller") {
    appendLog({ event: "error", message: "Viewer only — cannot start test" });
    return;
  }
  pendingFormData = formToObject(e.target);
  const avail = await fetchAvailability();

  if (!avail.ok) return;

  if (!avail.ev_available) {
    showEvModal(avail.ev?.operator);
    return;
  }

  if (!avail.lb2_available) {
    // LB2 unavailable is a hard failure — no skip popup.
    pendingFormData = null;
    appendLog({ event: "error", message: "LB2 not available" });
    setRunStatus("error", "LB2 not available");
    return;
  }

  await startTest(true);
});

$("jama-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  await startJamaTest();
});
$("jama-stop-btn")?.addEventListener("click", requestStopAutomation);

$("ev-modal-close")?.addEventListener("click", () => {
  $("ev-modal").classList.add("hidden");
});

async function requestStopAutomation() {
  if (role !== "controller") {
    appendLog({ event: "error", message: "Viewer only — cannot stop" });
    return;
  }
  const res = await fetch("/api/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: clientId }),
  });
  const data = await res.json();
  appendLog({ event: "stop_requested", message: data.message || "Stop requested" });
  setRunStatus("stopping", data.message || "Stopping…");
  if ($("stop-btn")) $("stop-btn").disabled = true;
  if ($("jama-stop-btn")) $("jama-stop-btn").disabled = true;
}

async function requestSafeShutdown(unit) {
  if (role !== "controller") {
    appendLog({ event: "error", message: "Viewer only — cannot safe-exit" });
    return;
  }
  const labels = {
    ev: "EV Simulator",
    lb2: "Load Bank-2",
    both: "EV Simulator and Load Bank-2",
  };
  const label = labels[unit] || unit;
  if (!window.confirm(`Safe exit ${label} now?\n\nThis will power down loads / release the session.`)) {
    return;
  }
  appendLog({ event: "safe_shutdown_click", unit, message: `User requested safe exit: ${label}` });
  setRunStatus("stopping", `Safe exiting ${label}…`);
  const res = await fetch("/api/safe-shutdown", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ unit, client_id: clientId }),
  });
  const data = await res.json();
  appendLog({ event: "safe_shutdown_accepted", ...data });
  if (!data.ok) {
    setRunStatus("error", data.error || "Safe shutdown failed to start");
  }
}

$("stop-btn")?.addEventListener("click", requestStopAutomation);
$("stop-automation-btn")?.addEventListener("click", requestStopAutomation);
$("shutdown-ev-btn")?.addEventListener("click", () => requestSafeShutdown("ev"));
$("shutdown-lb2-btn")?.addEventListener("click", () => requestSafeShutdown("lb2"));
$("shutdown-both-btn")?.addEventListener("click", () => requestSafeShutdown("both"));
$("save-log-btn")?.addEventListener("click", saveEventLog);
$("clear-log-btn")?.addEventListener("click", clearEventLog);

/* ── Log capture (CAN + SBX) & UART ─────────────────────────────────────── */
const _runStopState = { unified: false, uart: false, labCap: false };
let _lastUnifiedSession = "";
let _captureGateways = [];
let _captureGatewayMeta = { password_configured: false };
let _captureLogTailBusy = false;
const _CAP_LOG_LIVE_LINES = 320;
const _captureLogState = {
  can: { file: "", read_byte: 0, latest_log_ts: "", file_mtime: "" },
  modbus: { file: "", read_byte: 0, latest_log_ts: "", file_mtime: "" },
};
const _captureLogStickBottom = { can: true, modbus: true };

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function captureGatewaySelectEls() {
  return Array.from(document.querySelectorAll("select.capture-gateway-serial"));
}

function captureGatewaySerial() {
  for (const sel of captureGatewaySelectEls()) {
    const v = (sel.value || "").trim();
    if (v) return v;
  }
  return "";
}

function syncCaptureGatewaySerials(fromEl, value) {
  captureGatewaySelectEls().forEach((el) => {
    if (el !== fromEl) el.value = value;
  });
  updateCaptureGatewayHint();
  try {
    if (value) localStorage.setItem("captureGatewaySerial", value);
    else localStorage.removeItem("captureGatewaySerial");
  } catch (_e) { /* ignore */ }
}

function captureGatewayPayload() {
  const serial = captureGatewaySerial();
  if (!serial) return null;
  return { gateway_serial: serial };
}

function updateCaptureGatewayHint(overrideText, isError) {
  const serial = captureGatewaySerial();
  const text = overrideText || (!serial
    ? "Select a gateway serial (listed in GATEWAY_SERIALS on the server)."
    : (() => {
      const g = _captureGateways.find((x) => x.serial === serial);
      if (g && g.serial === "__local_capture__") {
        const pwdNote = _captureGatewayMeta.password_configured === false
          ? " · set CAPTURE_SSH_PASS or PANEL_SSH_PASS"
          : "";
        return `Local capture → ${g.user_name}@${g.host} (FT232R / CAN on lab cell)${pwdNote}`;
      }
      if (!g || !g.configured) {
        return g
          ? `${serial}: add gateway_serial + user_name under panel IP in panels_config.yml`
          : `${serial}: not found in panels_config.yml`;
      }
      const pwdNote = _captureGatewayMeta.password_configured === false
        ? " · set PANEL_SSH_PASS in config.env"
        : "";
      const cfgNote = _captureGatewayMeta.config_exists === false
        ? ` · missing ${_captureGatewayMeta.config_path || "panels_config.yml"}`
        : "";
      return `Log capture → ${g.user_name}@${g.host}${g.name ? ` (${g.name})` : ""}${pwdNote}${cfgNote}`;
    })());
  const color = isError || (serial && _captureGateways.find((x) => x.serial === serial && !x.configured))
    ? "#f85149"
    : "#8b949e";
  document.querySelectorAll(".capture-gateway-hint").forEach((hint) => {
    hint.style.color = color;
    hint.textContent = text;
  });
}

async function loadCaptureGateways() {
  const errBox = $("cap-unified-errors");
  try {
    const res = await fetch("/api/capture/gateways");
    const j = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = j.error || `Gateway list failed (${res.status})`;
      if (errBox) errBox.textContent = msg;
      updateCaptureGatewayHint(msg, true);
      return;
    }
    if (j.ok === false && j.error) {
      const msg = String(j.error);
      if (errBox) errBox.textContent = msg;
      updateCaptureGatewayHint(msg, true);
      return;
    }
    _captureGatewayMeta = {
      password_configured: j.password_configured !== false,
      config_path: j.config_path || "",
      config_exists: j.config_exists !== false,
    };
    _captureGateways = Array.isArray(j.gateways) ? j.gateways : [];
    let prev = "";
    try { prev = localStorage.getItem("captureGatewaySerial") || captureGatewaySerial(); } catch (_e) { /* */ }
    const options = `<option value="">— select gateway serial —</option>${
      _captureGateways.map((g) => {
        const label = g.name ? `${g.serial === "__local_capture__" ? g.name : g.serial + " (" + g.name + ")"}` : g.serial;
        const warn = g.configured ? "" : " ⚠";
        const display = g.serial === "__local_capture__" ? g.name || "Local capture" : label;
        return `<option value="${escapeHtml(g.serial)}">${escapeHtml(display)}${warn}</option>`;
      }).join("")
    }`;
    captureGatewaySelectEls().forEach((sel) => {
      sel.innerHTML = options;
      if (prev && _captureGateways.some((g) => g.serial === prev)) sel.value = prev;
      else if (_captureGateways.length === 1) sel.value = _captureGateways[0].serial;
    });
    if (!_captureGateways.length) {
      const msg = j.load_error
        || "No gateways — set GATEWAY_SERIALS and/or CAPTURE_SSH_* in config.env, then restart: ./run_dashboard.sh";
      if (errBox) errBox.textContent = msg;
      updateCaptureGatewayHint(msg, true);
      return;
    }
    if (errBox) errBox.textContent = j.load_error && !j.config_exists ? String(j.load_error) : "";
    updateCaptureGatewayHint();
  } catch (e) {
    const msg = `Could not load gateways: ${e}`;
    if (errBox) errBox.textContent = msg;
    updateCaptureGatewayHint(msg, true);
  }
}

captureGatewaySelectEls().forEach((sel) => {
  sel.addEventListener("change", () => syncCaptureGatewaySerials(sel, sel.value));
});

function _setCapBadge(id, running) {
  const el = $(id);
  if (!el) return;
  el.textContent = running ? "running" : "idle";
  el.className = running ? "badge badge-run" : "badge badge-idle";
}

function _captureLogEl(kind) {
  return $(kind === "can" ? "cap-can-live-log" : "cap-modbus-live-log");
}

function _captureLogNearBottom(el) {
  if (!el) return true;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 48;
}

function _renderCaptureLogTail(kind, payload) {
  const el = _captureLogEl(kind);
  const fileEl = $(kind === "can" ? "cap-can-log-file" : "cap-modbus-log-file");
  const ageEl = $(kind === "can" ? "cap-can-log-age" : "cap-modbus-log-age");
  const st = _captureLogState[kind];
  if (!el || !st) return;
  const stick = _captureLogStickBottom[kind] !== false;
  const near = _captureLogNearBottom(el);
  if (payload && payload.text != null) {
    const text = String(payload.text);
    if (el.textContent !== text) el.textContent = text || "(empty)";
  }
  if (payload && payload.file && fileEl) {
    fileEl.textContent = payload.file;
    fileEl.title = payload.file;
    st.file = payload.file;
  }
  if (payload && payload.latest_log_ts) st.latest_log_ts = payload.latest_log_ts;
  if (payload && payload.file_mtime) st.file_mtime = payload.file_mtime;
  if (ageEl) {
    ageEl.classList.remove("fresh", "stale");
    if (st.latest_log_ts) {
      ageEl.textContent = `line ${String(st.latest_log_ts).slice(11, 19)}`;
      // Approximate freshness from wall-clock of the last log line (date + time).
      const parsed = Date.parse(String(st.latest_log_ts).replace(" ", "T"));
      if (Number.isFinite(parsed)) {
        const ageSec = (Date.now() - parsed) / 1000;
        ageEl.classList.add(ageSec <= 8 ? "fresh" : "stale");
      } else {
        ageEl.classList.add("fresh");
      }
    } else if (st.file_mtime) {
      ageEl.textContent = `file ${st.file_mtime}`;
      ageEl.classList.add("stale");
    } else {
      ageEl.textContent = "—";
      ageEl.classList.add("stale");
    }
  }
  if (stick && near) el.scrollTop = el.scrollHeight;
}

async function syncCaptureLog(kind) {
  const st = _captureLogState[kind];
  const el = _captureLogEl(kind);
  if (!st) return;
  if (el) _captureLogStickBottom[kind] = _captureLogNearBottom(el);
  const running = !!_runStopState.unified;
  const lines = running ? _CAP_LOG_LIVE_LINES : 0;
  let url = `/api/logs/unified/tail?kind=${encodeURIComponent(kind)}`;
  url += lines > 0 ? `&lines=${lines}` : `&since_byte=${st.read_byte}`;
  let j;
  try {
    j = await (await fetch(url)).json();
  } catch (_) {
    return;
  }
  if (!j || !j.ok) return;
  if (typeof j.next_byte === "number") st.read_byte = j.next_byte;
  _renderCaptureLogTail(kind, {
    text: lines > 0 ? j.chunk : undefined,
    append: lines > 0 ? undefined : j.chunk,
    file: j.file,
    latest_log_ts: j.latest_log_ts,
    file_mtime: j.file_mtime,
  });
  if (!lines && j.chunk) {
    if (!el.textContent || el.textContent.startsWith("(start capture") || el.textContent === "(empty)") {
      el.textContent = "";
    }
    el.insertAdjacentText("beforeend", j.chunk);
    if (_captureLogStickBottom[kind]) el.scrollTop = el.scrollHeight;
  }
}

async function pollUnifiedLogTails() {
  if (_captureLogTailBusy) return;
  _captureLogTailBusy = true;
  try {
    await syncCaptureLog("can");
    await syncCaptureLog("modbus");
  } finally {
    _captureLogTailBusy = false;
  }
}

function _renderUnifiedFiles(files, sessionId) {
  const box = $("log-files-unified");
  if (!box) return;
  const list = Array.isArray(files) ? files : [];
  if (!list.length) {
    box.innerHTML = "<p class='hint-text'>No files yet</p>";
    return;
  }
  box.innerHTML = list.map((rel) => {
    const href = `/api/logs/unified/download?session_id=${encodeURIComponent(sessionId || "")}&rel=${encodeURIComponent(rel)}`;
    return `<a class="uart-dl" href="${href}">${rel}</a>`;
  }).join("");
}

async function pollUnifiedStatus() {
  let j;
  try {
    j = await (await fetch("/api/logs/unified/status")).json();
  } catch (_) {
    return;
  }
  _runStopState.unified = !!j.running;
  _setCapBadge("cap-unified-badge", j.running);
  const warnList = Array.isArray(j.warnings) ? j.warnings.filter(Boolean) : [];
  const skipCan = !!j.skip_can;
  const sbxDown = j.modbus_ok === false;
  const canDown = !skipCan && j.can_ok === false;
  // Hard fail only when SBX is down or there is a fatal error. Missing PCAN is a
  // soft warning — Modbus/SBX can still stream (common on walls without PCAN).
  const hardFail = !!(j.error || sbxDown);
  if (j.running && hardFail) setNavBadge("nav-badge-capture", "failed", "failed");
  else if (j.running && canDown) setNavBadge("nav-badge-capture", "sbx-only", "running");
  else if (j.running) setNavBadge("nav-badge-capture", "running", "running");
  else if (hardFail) setNavBadge("nav-badge-capture", "failed", "failed");
  else setNavBadge("nav-badge-capture", "idle", "");
  if (j.session_id) _lastUnifiedSession = j.session_id;
  const relays = j.can_relays || {};
  if ($("cap-can-mid")) $("cap-can-mid").textContent = relays.mid_relay || "—";
  if ($("cap-can-der")) $("cap-can-der").textContent = relays.der_relay || "—";
  if ($("cap-can-nft")) $("cap-can-nft").textContent = relays.nft_relay || "—";
  if ($("cap-mod-led")) {
    $("cap-mod-led").textContent = j.drive_status || j.mid_led_mode_request || "—";
  }
  if (skipCan) {
    _setStreamHealthMasked("cap-can-health", "CAN");
  } else {
    _setStreamHealth("cap-can-health", j.can_ok, "CAN");
  }
  _setStreamHealth("cap-modbus-health", j.modbus_ok, "SBX");
  const alertBox = $("cap-unified-alerts");
  if (alertBox) {
    const lines = [];
    if (j.error) lines.push(String(j.error));
    warnList.forEach((w) => lines.push(String(w)));
    if (lines.length) {
      alertBox.hidden = false;
      alertBox.textContent = lines.join("\n\n");
    } else {
      alertBox.hidden = true;
      alertBox.textContent = "";
    }
  }
  _renderUnifiedFiles(j.active_files || j.files || [], j.session_id);
  if (j.running || j.session_id) await pollUnifiedLogTails();
}

function _setStreamHealth(elId, ok, label) {
  const el = $(elId);
  if (!el) return;
  el.classList.remove("is-ok", "is-bad", "is-unknown");
  if (ok === true) {
    el.textContent = `${label} OK`;
    el.classList.add("is-ok");
    el.title = `${label} capture active`;
  } else if (ok === false) {
    el.textContent = `${label} DOWN`;
    el.classList.add("is-bad");
    el.title = `${label} not connected or not capturing`;
  } else {
    el.textContent = "—";
    el.classList.add("is-unknown");
    el.title = "";
  }
}

function _setStreamHealthMasked(elId, label) {
  const el = $(elId);
  if (!el) return;
  el.classList.remove("is-ok", "is-bad", "is-unknown");
  el.textContent = `${label} SKIPPED`;
  el.classList.add("is-unknown");
  el.title = `${label} masked (SBX/Modbus only)`;
}

let _panelPollBusy = false;
async function pollPanelCurrent() {
  if (_panelPollBusy) return;
  _panelPollBusy = true;
  try {
    const j = await (await fetch("/api/logs/unified/panel_current")).json();
    let total = null;
    const pc = j && j.panel_current;
    if (pc && Number.isFinite(Number(pc.total_a))) {
      total = Number(pc.total_a);
    } else if (_runStopState.labCap) {
      // No CAN RMS (PCAN missing / masked) — use gateway panel sample from lab capture.
      try {
        const lab = await (await fetch("/api/lab-capture/status")).json();
        const combined = lab && lab.last && lab.last.panel && lab.last.panel.combined_a;
        if (combined != null && Number.isFinite(Number(combined))) {
          total = Number(combined);
        }
      } catch (_) {
        /* ignore */
      }
    }
    if (total != null) {
      if (gauges.panel) gauges.panel.set(total, { max: panelCapacityA });
      updatePanelPlot(total);
      const ov = $("ov-panel");
      if (ov) ov.textContent = `${fmtNum(total, 1)} A`;
    }
  } catch (_) {
    /* ignore transient poll errors */
  } finally {
    _panelPollBusy = false;
  }
}

async function unifiedStart() {
  const gwPayload = captureGatewayPayload();
  const alertBox = $("cap-unified-alerts");
  const startBtn = $("btn-cap-unified-start");
  if (!gwPayload) {
    if (alertBox) {
      alertBox.hidden = false;
      alertBox.textContent = "Select a gateway serial number first.";
    }
    return;
  }
  if (alertBox) {
    alertBox.hidden = true;
    alertBox.textContent = "";
  }
  if (startBtn) startBtn.disabled = true;
  _setCapBadge("cap-unified-badge", true);
  setNavBadge("nav-badge-capture", "starting", "running");
  const skipCanEl = $("cap-unified-skip-can");
  const payload = {
    ...gwPayload,
    skip_can: skipCanEl ? !!skipCanEl.checked : true,
  };
  let j;
  try {
    const res = await fetchWithTimeout("/api/logs/unified/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 20000);
    j = await res.json();
  } catch (err) {
    _setCapBadge("cap-unified-badge", false);
    setNavBadge("nav-badge-capture", "failed", "failed");
    if (alertBox) {
      alertBox.hidden = false;
      alertBox.textContent = err && err.name === "AbortError"
        ? "Start capture got no reply in 20s (service busy). Restart: sudo systemctl restart drive-dashboard"
        : `Start failed: ${err.message || err}`;
    }
    if (startBtn) startBtn.disabled = false;
    return;
  }
  if (startBtn) startBtn.disabled = false;
  if (!j.ok) {
    _setCapBadge("cap-unified-badge", false);
    if (alertBox) {
      alertBox.hidden = false;
      alertBox.textContent = j.error || "Start failed";
    }
    return;
  }
  _lastUnifiedSession = j.session_id || "";
  ["can", "modbus"].forEach((k) => {
    _captureLogState[k].read_byte = 0;
    const el = _captureLogEl(k);
    if (el) el.textContent = "(waiting for log file…)";
  });
  _setCapBadge("cap-unified-badge", true);
  appendLog({ event: "capture_unified_start", ...j });
  pollUnifiedStatus();
}

async function unifiedStop() {
  try {
    const j = await (await fetchWithTimeout("/api/logs/unified/stop", { method: "POST" }, 15000)).json();
    _setCapBadge("cap-unified-badge", false);
    appendLog({ event: "capture_unified_stop", ...j });
  } catch (err) {
    appendLog({ event: "capture_unified_stop", ok: false, message: String(err.message || err) });
  }
  pollUnifiedStatus();
}

function _renderUartLinks(status) {
  const box = $("cap-uart-links");
  if (!box) return;
  const logs = status.log_files || {};
  const caps = status.capture_files || {};
  const parts = [];
  Object.keys(logs).forEach((k) => {
    const p = logs[k];
    parts.push(`<a class="uart-dl" href="/api/logs/uart/download?path=${encodeURIComponent(p)}">${escapeHtml(k)} — uart log</a>`);
  });
  Object.keys(caps).forEach((k) => {
    const p = caps[k];
    parts.push(`<a class="uart-dl" href="/api/logs/uart/download?path=${encodeURIComponent(p)}">${escapeHtml(k)} — capture</a>`);
  });
  box.innerHTML = parts.length ? parts.join("") : "<p class='hint-text'>No UART files yet — start UART capture</p>";
}

async function pollUartStatus() {
  let j;
  try {
    j = await (await fetch("/api/logs/uart/status")).json();
  } catch (_) {
    return;
  }
  _runStopState.uart = !!j.running;
  _setCapBadge("cap-uart-badge", j.running);
  _renderUartLinks(j);
}

async function uartStart() {
  const gwPayload = captureGatewayPayload();
  const err = $("cap-uart-errors");
  if (!gwPayload) {
    if (err) err.textContent = "Select a gateway serial number first.";
    return;
  }
  if (err) err.textContent = "";
  const raw = ($("cap-uart-strings") && $("cap-uart-strings").value) || "";
  const strings = raw.split("\n").map((s) => s.trim()).filter(Boolean);
  const res = await fetch("/api/logs/uart/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...gwPayload, strings }),
  });
  const j = await res.json();
  if (!j.ok) {
    if (err) err.textContent = j.error || "UART start failed";
    return;
  }
  _setCapBadge("cap-uart-badge", true);
  appendLog({ event: "capture_uart_start", ...j });
  pollUartStatus();
}

async function uartStop() {
  const j = await (await fetch("/api/logs/uart/stop", { method: "POST" })).json();
  _setCapBadge("cap-uart-badge", false);
  appendLog({ event: "capture_uart_stop", ...j });
  pollUartStatus();
}

async function loadCaptureTargets() {
  await loadCaptureGateways();
}

function openCaptureLogFullscreen(kind) {
  const src = _captureLogEl(kind);
  const overlay = $("capture-log-fullscreen");
  const title = $("cap-fs-title");
  const body = $("cap-fs-body");
  if (!src || !overlay || !body) return;
  if (title) title.textContent = kind === "can" ? "CAN bus log" : "Modbus / SBX log";
  body.textContent = src.textContent || "(empty)";
  overlay.classList.remove("hidden");
  body.scrollTop = body.scrollHeight;
}

function closeCaptureLogFullscreen() {
  const overlay = $("capture-log-fullscreen");
  if (overlay) overlay.classList.add("hidden");
}

function initCaptureExpand() {
  document.querySelectorAll("[data-cap-expand]").forEach((btn) => {
    btn.addEventListener("click", () => openCaptureLogFullscreen(btn.dataset.capExpand));
  });
  $("cap-fs-close")?.addEventListener("click", closeCaptureLogFullscreen);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeCaptureLogFullscreen();
  });
}

if ($("btn-cap-unified-start")) $("btn-cap-unified-start").addEventListener("click", unifiedStart);
if ($("btn-cap-unified-stop")) $("btn-cap-unified-stop").addEventListener("click", unifiedStop);
if ($("btn-cap-uart-start")) $("btn-cap-uart-start").addEventListener("click", uartStart);
if ($("btn-cap-uart-stop")) $("btn-cap-uart-stop").addEventListener("click", uartStop);
$("sse-reload-btn")?.addEventListener("click", () => window.location.reload());

loadCaptureTargets();
initCaptureExpand();
setInterval(pollUnifiedStatus, 3500);
setInterval(pollPanelCurrent, 1000);
setInterval(pollUartStatus, 3000);
setInterval(() => {
  if (_runStopState.unified) pollUnifiedLogTails();
}, 800);

window.addEventListener("beforeunload", () => {
  if (role === "controller" && clientId) {
    const body = JSON.stringify({ action: "release", client_id: clientId });
    if (navigator.sendBeacon) {
      navigator.sendBeacon("/api/role", new Blob([body], { type: "application/json" }));
    }
  }
});

function _labCapSelectedStreams() {
  const streams = [];
  if ($("lab-cap-pilot")?.checked) streams.push("pilot");
  if ($("lab-cap-ev")?.checked) streams.push("ev_loads");
  if ($("lab-cap-lb2")?.checked) streams.push("lb2");
  if ($("lab-cap-panel")?.checked) streams.push("panel");
  return streams;
}

function _labCapPilotFields(last) {
  const loads = (last && last.ev_loads) || {};
  const pilot = (last && last.pilot) || {};
  return {
    mode: loads.pilot_mode ?? pilot.pilot_mode,
    applied: loads.applied_current_a ?? pilot.applied_current_a,
    adv: loads.adv_current_a ?? pilot.adv_current_a,
    duty: loads.duty_cycle_pct ?? pilot.duty_cycle_pct,
    maxA: loads.max_current_a ?? pilot.max_current_a,
  };
}

function _renderLabCapStatus(j) {
  _setCapBadge("lab-cap-badge", !!j.running);
  const notes = $("lab-cap-notes");
  if (notes) {
    const bits = [];
    if (j.message) bits.push(String(j.message));
    if (Array.isArray(j.notes)) bits.push(...j.notes.map(String));
    const err = j.last && j.last.errors;
    if (err && typeof err === "object") {
      Object.entries(err).forEach(([k, v]) => bits.push(`${k}: ${v}`));
    }
    notes.textContent = bits.join(" · ");
  }
  const last = (j.last) || {};
  const pilot = _labCapPilotFields(last);
  const lb2 = last.lb2 || {};
  const panel = last.panel || {};
  if ($("lab-cap-live-mode")) {
    $("lab-cap-live-mode").textContent =
      pilot.mode != null && pilot.mode !== ""
        ? String(pilot.mode).toUpperCase()
        : "—";
  }
  if ($("lab-cap-live-ev")) {
    const a = pilot.applied ?? pilot.adv;
    $("lab-cap-live-ev").textContent =
      a != null && Number.isFinite(Number(a)) ? `${Number(a).toFixed(2)} A` : "—";
  }
  if ($("lab-cap-live-duty")) {
    const duty = pilot.duty;
    const adv = pilot.adv;
    if (duty != null || adv != null) {
      const d = duty != null ? `${Number(duty).toFixed(1)}%` : "—";
      const a = adv != null ? `${Number(adv).toFixed(1)}A` : "—";
      $("lab-cap-live-duty").textContent = `${d} / ${a}`;
    } else {
      $("lab-cap-live-duty").textContent = "—";
    }
  }
  if ($("lab-cap-live-lb2")) {
    $("lab-cap-live-lb2").textContent =
      lb2.total_amps != null ? `${Number(lb2.total_amps).toFixed(2)} A` : "—";
  }
  if ($("lab-cap-live-panel")) {
    $("lab-cap-live-panel").textContent =
      panel.combined_a != null ? `${Number(panel.combined_a).toFixed(2)} A` : "—";
  }
  // Mirror into header gauges while lab capture is active (no pipeline SSE).
  if (j.running) {
    if (pilot.mode != null || pilot.duty != null || pilot.applied != null || pilot.adv != null) {
      renderEv({
        pilot_mode: pilot.mode,
        duty_cycle_pct: pilot.duty,
        applied_current_a: pilot.applied,
        adv_current_a: pilot.adv,
        max_current_a: pilot.maxA,
      });
    }
    if (lb2.total_amps != null && $("ov-lb2")) {
      $("ov-lb2").textContent = `${Number(lb2.total_amps).toFixed(1)} A`;
    }
    if (panel.combined_a != null && Number.isFinite(Number(panel.combined_a))) {
      const total = Number(panel.combined_a);
      if (gauges.panel) gauges.panel.set(total, { max: panelCapacityA });
      updatePanelPlot(total);
      const ov = $("ov-panel");
      if (ov) ov.textContent = `${fmtNum(total, 1)} A`;
    }
  }
  const links = $("lab-cap-links");
  if (links) {
    const files = j.files || {};
    const urls = j.download_urls || {};
    const parts = Object.keys(files).map((name) => {
      const href = urls[name] || `/api/lab-capture/download?stream=${encodeURIComponent(name)}`;
      return `<a class="btn secondary" href="${href}">${name}</a>`;
    });
    if (Object.keys(files).length) {
      parts.push(`<a class="btn primary" href="/api/lab-capture/download?stream=zip">Download zip</a>`);
    }
    links.innerHTML = parts.join(" ") || "<span class='hint-text'>No files yet</span>";
  }
}

async function labCapRefresh() {
  try {
    const j = await (await fetch("/api/lab-capture/status")).json();
    _renderLabCapStatus(j);
    return j;
  } catch (_) {
    return null;
  }
}

async function labCapStart() {
  const streams = _labCapSelectedStreams();
  if (!streams.length) {
    const notes = $("lab-cap-notes");
    if (notes) notes.textContent = "Select at least one stream.";
    return;
  }
  const takeControl = !!$("lab-cap-take-control")?.checked;
  const j = await (await fetch("/api/lab-capture/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      streams,
      interval_sec: 1.0,
      take_control: takeControl,
    }),
  })).json();
  _renderLabCapStatus(j);
}

async function labCapStop() {
  const j = await (await fetch("/api/lab-capture/stop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ logout: true }),
  })).json();
  _renderLabCapStatus(j);
}

if ($("btn-lab-cap-start")) $("btn-lab-cap-start").addEventListener("click", labCapStart);
if ($("btn-lab-cap-stop")) $("btn-lab-cap-stop").addEventListener("click", labCapStop);
setInterval(() => {
  // Card is shown via CSS (not .hidden); poll whenever Log Capture view is active
  // or a session is already running so header gauges stay live.
  const onCapture = document.body.dataset.dashView === "capture";
  if (onCapture || _runStopState.labCap) {
    labCapRefresh().then((j) => {
      _runStopState.labCap = !!(j && j.running);
    });
  }
}, 2000);
labCapRefresh().then((j) => {
  _runStopState.labCap = !!(j && j.running);
});

ensureClientId();
bindNav();
initGauges();
initConfigGroups();
setPipeHasJob(false);
renderPipelineStepper("");
connectEvents();
fetchAvailability();
syncRole("claim").catch((err) => {
  const badge = $("role-badge");
  if (badge) {
    badge.className = "role-badge claiming";
    badge.textContent = "Offline";
  }
  console.warn("role sync failed", err);
});
try {
  initCharts();
} catch (err) {
  console.warn("charts init failed", err);
}
setInterval(() => {
  syncRole("heartbeat").catch(() => {});
}, 10000);
setInterval(updatePhaseTimerUi, 250);
