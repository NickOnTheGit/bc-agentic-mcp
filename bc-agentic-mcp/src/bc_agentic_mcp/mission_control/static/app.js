/* BC Mission Control — cockpit client (vanilla JS, no deps) */
"use strict";

const $ = (id) => document.getElementById(id);
const POLL_OVERVIEW_MS = 4000;
const POLL_ITEM_MS = 2500;

// Phases whose next step is model judgment (spec/design/code) — agent territory.
const JUDGMENT_PHASES = new Set([
  "item_received", "spec_written", "design_planned", "code_context_built",
  "tasks_broken_down", "review_prepared", "decision_recorded",
  "feature_captured", "feature_refined", "feature_planned", "item_refined",
  "root_cause_identified",
]);
const JUDGMENT_HINTS = {
  item_received: "Write the spec (bc_write_spec) from the captured context.",
  root_cause_identified: "Diagnosis recorded — write the bugfix spec (bc_write_spec) carrying the regression requirement.",
  spec_written: "Plan the technical design (bc_plan_design).",
  design_planned: "Build the code read-context (bc_read_code_context).",
  code_context_built: "Break the design into tasks (bc_breakdown_tasks).",
  tasks_broken_down: "Prepare the review packet (bc_prepare_review), then request plan approval.",
  review_prepared: "Request the plan approval gate (bc_request_approval).",
  decision_recorded: "Plan approved — implement via bc_implement_write.",
  feature_captured: "Refine the feature (bc_refine_feature).",
  feature_refined: "Plan the feature (bc_plan_feature).",
  feature_planned: "Refine child items (bc_refine_item) and launch them as missions.",
  item_refined: "Start the delivery lifecycle for the refined item.",
  root_cause_identified: "Write the fix spec (bc_write_spec) — the symptom regression requirement is mandatory.",
};

const state = {
  selected: null,
  projectRoot: "",
  overviewTimer: null,
  itemTimer: null,
  lastPulse: null,
  lastOverviewSig: "",
  lastPulseSig: "",
  featureLoadedFor: null,
  notifySnapshot: {},   // spec -> "phase|needsCount" for notification diffing
  autopilot: false,
  autopilotBusy: false,
};

/* ---------------- presets (localStorage, this browser only) ---------------- */
const PRESET_FIELDS = {
  "ps-container": "test_container_name",
  "ps-extension": "test_extension_id",
  "ps-credenv": "credential_env",
  "ps-appfolder": "app_project_folder",
  "ps-org": "org_url",
  "ps-project": "project",
  "ps-repo": "repository",
  "ps-branch": "target_branch",
  "ps-wtbase": "worktrees_base",
  "ps-agentcmd": "agent_cmd",
};
function loadPresets() {
  try { return JSON.parse(localStorage.getItem("mc_presets") || "{}"); } catch { return {}; }
}
function savePresetsFromForm() {
  const presets = {};
  for (const [id, key] of Object.entries(PRESET_FIELDS)) {
    const v = $(id).value.trim();
    if (v) presets[key] = v;
  }
  localStorage.setItem("mc_presets", JSON.stringify(presets));
  // Server-side copy too — the routine scheduler and push-to-ADO run without a browser.
  fetch("/api/presets", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(presets) }).catch(() => {});
  toast("Presets saved — Advance, routines and ADO push use them automatically");
  $("presets-modal").hidden = true;
}
function openPresets() {
  const presets = loadPresets();
  for (const [id, key] of Object.entries(PRESET_FIELDS)) $(id).value = presets[key] || "";
  $("presets-modal").hidden = false;
}

/* ---------------- desktop notifications ---------------- */
function notifyEnabled() { return localStorage.getItem("mc_notify") === "1" && Notification.permission === "granted"; }
function paintNotifyBtn() { $("btn-notify").style.opacity = notifyEnabled() ? "1" : ".45"; }
async function toggleNotify() {
  if (notifyEnabled()) {
    localStorage.setItem("mc_notify", "0");
    toast("Notifications off");
  } else {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") { toast("Browser blocked notifications", true); return paintNotifyBtn(); }
    localStorage.setItem("mc_notify", "1");
    new Notification("Zig:Mission Control", { body: "You'll be pinged when a mission needs you." });
  }
  paintNotifyBtn();
}
function maybeNotify(spec, phase, needsCount) {
  const sig = `${phase}|${needsCount}`;
  const prev = state.notifySnapshot[spec];
  state.notifySnapshot[spec] = sig;
  if (prev === undefined || prev === sig || !notifyEnabled()) return;
  const [prevPhase, prevNeeds] = prev.split("|");
  if (needsCount > Number(prevNeeds)) {
    new Notification(`◉ ${spec} needs you`, { body: `${needsCount} item(s) waiting: questions, approvals or blockers.` });
  } else if (phase !== prevPhase) {
    new Notification(`▶ ${spec}`, { body: `Phase: ${phaseLabel(prevPhase)} → ${phaseLabel(phase)}` });
  }
}

/* ---------------- helpers ---------------- */
async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(body.error || res.statusText || "request failed");
    err.detail = body; // {tool, error, reason, hint, stage, retryable}
    throw err;
  }
  return body;
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = isErr ? "err" : "";
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.hidden = true; }, 3500);
}
function phaseLabel(p) { return (p || "new").replaceAll("_", " "); }
function setLive(ok) {
  $("live-dot").className = "dot " + (ok ? "live" : "down");
  $("live-label").textContent = ok ? "LIVE" : "OFFLINE";
}
function showActionOutput(obj) {
  const el = $("action-output");
  el.hidden = false;
  el.classList.remove("error-card");
  el.textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
}
function showActionError(detail) {
  const el = $("action-output");
  el.hidden = false;
  el.classList.add("error-card");
  const rows = [
    `<div class="err-title">⚠ ${esc(detail.tool || "action")} was blocked</div>`,
    `<div class="err-msg">${esc(detail.error || "Unknown error")}</div>`,
  ];
  if (detail.reason) rows.push(`<div class="err-row"><b>Why:</b> ${esc(detail.reason)}</div>`);
  if (detail.stage) rows.push(`<div class="err-row"><b>Current stage:</b> ${esc(detail.stage)} — this tool belongs to a different lifecycle stage.</div>`);
  if (detail.hint) rows.push(`<div class="err-row"><b>What to do:</b> ${esc(detail.hint)}</div>`);
  rows.push(`<div class="err-row muted">Tip: hit 🛰 Guidance — the server tells you the exact next tool for this item.</div>`);
  el.innerHTML = rows.join("");
}

/* Minimal, escaped markdown rendering for artifact viewing. */
function renderMd(text) {
  const lines = esc(text).split("\n");
  const out = [];
  let inCode = false, inList = false;
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      out.push(inCode ? "</pre>" : "<pre>");
      inCode = !inCode;
      continue;
    }
    if (inCode) { out.push(line); continue; }
    let l = line
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    const h = l.match(/^(#{1,4})\s+(.*)$/);
    const isLi = /^\s*[-*]\s+/.test(l);
    if (isLi && !inList) { out.push("<ul>"); inList = true; }
    if (!isLi && inList) { out.push("</ul>"); inList = false; }
    if (h) out.push(`<h${h[1].length}>${h[2]}</h${h[1].length}>`);
    else if (isLi) out.push(`<li>${l.replace(/^\s*[-*]\s+/, "")}</li>`);
    else if (l.trim() === "") out.push("<br>");
    else out.push(`<p>${l}</p>`);
  }
  if (inList) out.push("</ul>");
  if (inCode) out.push("</pre>");
  return out.join("\n");
}

/* ---------------- overview / missions list ---------------- */
async function refreshOverview() {
  try {
    const data = await api("/api/overview");
    setLive(true);
    state.projectRoot = data.project_root;
    $("project-root").textContent = data.project_root;
    const sig = JSON.stringify(data.items) + "::" + state.selected;
    if (sig === state.lastOverviewSig) return; // no change — keep DOM stable
    state.lastOverviewSig = sig;
    $("mission-count").textContent = data.items.length;
    const list = $("mission-list");
    list.innerHTML = "";
    for (const item of data.items) {
      const li = document.createElement("li");
      li.className = item.name === state.selected ? "selected" : "";
      const flags = [];
      if (item.is_intake) flags.push(`<span class="badge">🧠 intake</span>`);
      if (item.is_feature) flags.push(`<span class="badge">🧩 feature</span>`);
      if (item.lane === "bugfix") flags.push(`<span class="badge hot">🐞 bug</span>`);
      if (item.open_questions) flags.push(`<span class="badge hot">❓ ${item.open_questions}</span>`);
      if (item.pending_approvals) flags.push(`<span class="badge hot">🖊 ${item.pending_approvals}</span>`);
      li.innerHTML = `
        <div class="mission-row-top">
          <span class="mission-name">${esc(item.name)}</span>
          <span class="badge">${esc(phaseLabel(item.phase))}</span>
        </div>
        <div class="mini-track"><div class="mini-fill" style="width:${item.percent}%"></div></div>
        ${flags.length ? `<div class="mission-flags">${flags.join("")}</div>` : ""}`;
      li.onclick = () => selectMission(item.name);
      list.appendChild(li);
    }
  } catch {
    setLive(false);
  }
}

/* ---------------- mission selection + pulse ---------------- */
function selectMission(name) {
  state.selected = name;
  state.lastOverviewSig = "";
  state.lastPulseSig = "";
  state.featureLoadedFor = null;
  state.autopilot = false;
  paintAutopilot();
  $("dispatch-strip").hidden = true;
  $("empty-state").hidden = true;
  $("mission-view").hidden = false;
  $("action-output").hidden = true;
  $("pr-out").hidden = true;
  refreshOverview();
  refreshItem(true);
  clearInterval(state.itemTimer);
  state.itemTimer = setInterval(refreshItem, POLL_ITEM_MS);
}

async function refreshItem(scrollReset) {
  if (!state.selected) return;
  let pulse;
  try {
    pulse = await api(`/api/item/${encodeURIComponent(state.selected)}/pulse`);
  } catch (e) { return; }
  if (!pulse.exists) return;
  state.lastPulse = pulse;
  const sig = JSON.stringify(pulse);
  if (sig === state.lastPulseSig && scrollReset !== true) return; // unchanged
  state.lastPulseSig = sig;

  $("m-name").textContent = pulse.name;
  $("m-purpose").textContent = pulse.charter.purpose || "";
  const prog = pulse.progress;
  $("m-pct").textContent = `${prog.percent}%`;
  $("m-bar").style.width = `${prog.percent}%`;
  $("m-phase").textContent = phaseLabel(prog.current);

  // pipeline chips
  const ol = $("pipeline");
  ol.innerHTML = "";
  for (const p of prog.pipeline) {
    const li = document.createElement("li");
    li.textContent = phaseLabel(p);
    if (p === prog.current) li.className = "current";
    else if (prog.done.includes(p)) li.className = "done";
    ol.appendChild(li);
  }

  renderNeedsYou(pulse);
  renderArtifacts(pulse.artifacts);
  renderEvents(pulse.events);
  renderReadiness(pulse);
  renderPrPanel(pulse);
  loadFeaturePanel(pulse);
  // Plan-gate form: only while the item is shaped (plan stage, not a feature/intake folder).
  $("plangate-panel").hidden = !(pulse.stage === "plan" && !pulse.is_feature && !pulse.is_intake);
  // Consistency verdict strip inside the plan gate.
  if (!$("plangate-panel").hidden && pulse.consistency) {
    let strip = $("consistency-strip");
    if (!strip) {
      strip = document.createElement("div");
      strip.id = "consistency-strip";
      strip.className = "action-output";
      $("plangate-panel").appendChild(strip);
    }
    strip.hidden = false;
    const c = pulse.consistency;
    strip.innerHTML = `<div class="ready-row">${c.ok ? "✅" : "❌"} <b>Consistency: ${esc(c.status)}</b></div>`
      + (c.critical || []).map((x) => `<div class="ready-row blocker">• ${esc(x)}</div>`).join("")
      + (c.warnings || []).map((x) => `<div class="ready-row">⚠ ${esc(x)}</div>`).join("");
  }
  renderBugPanel(pulse);
  renderIntakePanel(pulse);
  const needsCount = pulse.clarifications.filter((q) => q.open && !pulse.clarifications_locked).length
    + pulse.approvals.filter((a) => a.status === "pending").length;
  maybeNotify(pulse.name, prog.current, needsCount);
  runAutopilot();
  // Auto-clarify is a plan-stage tool — grey it out when the server would block it.
  const ac = $("btn-autoclarify");
  ac.disabled = !!pulse.clarifications_locked;
  ac.title = pulse.clarifications_locked
    ? `Locked: clarification tools only run in the plan stage (item is in '${pulse.stage}')`
    : "Propose evidence-grounded answers to open questions";
  if (scrollReset === true) window.scrollTo(0, 0);
}

/* ---------------- readiness panel ---------------- */
function renderReadiness(pulse) {
  const panel = $("readiness-panel");
  const r = pulse.readiness;
  // Only meaningful once implementation starts — planning phases have no evidence yet.
  if (!r || pulse.is_feature || pulse.stage === "plan") { panel.hidden = true; return; }
  panel.hidden = false;
  $("readiness-badge").textContent = r.passed ? "PASSED" : `${r.coverage_pct}% covered`;
  $("readiness-badge").className = r.passed ? "badge ok-badge" : "badge hot";
  const rows = [];
  if (pulse.rubric) {
    const s = pulse.rubric.scores || {};
    rows.push(`<div class="ready-row">${pulse.rubric.passed ? "✅" : "⚠"} <b>Review rubric:</b> overall ${pulse.rubric.overall}
      <span class="muted small">(grounding ${s.grounding ?? "?"} · coverage ${s.coverage ?? "?"} · conventions ${s.conventions ?? "?"} · risk ${s.risk ?? "?"} — ${pulse.rubric.count} review(s))</span></div>`);
  }
  rows.push(`<div class="ready-row">${r.passed ? "✅" : "⏳"} Acceptance criteria: <b>${r.criteria_count}</b>,
    coverage <b>${r.coverage_pct}%</b> (evidence bar: ${esc(r.required_strength || "n/a")})</div>`);
  for (const [name, s] of Object.entries(r.validation_classes || {})) {
    if (!s.required) continue;
    rows.push(`<div class="ready-row">${s.ok ? "✅" : "❌"} ${esc(name)} validation${s.ok ? "" : ` — <span class="muted">${esc(s.reason)}</span>`}</div>`);
  }
  for (const b of r.blockers.slice(0, 6)) rows.push(`<div class="ready-row blocker">• ${esc(b)}</div>`);
  if (r.passed) rows.push(`<div class="ready-row ok-text">Gate passes — approvals for implement/complete will not be blocked.</div>`);
  $("readiness-body").innerHTML = rows.join("");
}

/* ---------------- feature roll-up panel ---------------- */
async function loadFeaturePanel(pulse) {
  const panel = $("feature-panel");
  if (!pulse.is_feature) { panel.hidden = true; state.featureLoadedFor = null; return; }
  panel.hidden = false;
  if (state.featureLoadedFor === pulse.name) return; // loaded once per selection
  state.featureLoadedFor = pulse.name;
  $("feature-body").textContent = "Loading roll-up…";
  try {
    const data = await api(`/api/item/${encodeURIComponent(pulse.name)}/feature-status`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    renderFeature(data.result || {});
  } catch (e) {
    $("feature-body").textContent = e.message;
  }
}
function renderFeature(fs) {
  $("feature-phase").textContent = (fs.feature_phase || "").replaceAll("_", " ");
  const rows = (fs.items || []).map((it) => {
    const phase = it.phase ? phaseLabel(it.phase) : "not started";
    const action = it.item_spec
      ? `<button class="btn small-btn" data-open="${esc(it.item_spec)}">open</button>`
      : `<button class="btn small-btn" data-launch="${esc(String(it.id))}">launch</button>`;
    return `<tr><td class="mono">#${esc(String(it.id))}</td><td>${esc(it.title)}</td>
      <td><span class="badge">${esc(phase)}</span></td><td>${action}</td></tr>`;
  }).join("");
  const next = fs.next_action
    ? `<p class="small" style="margin:8px 0 0"><b>Next:</b> ${esc(fs.next_action.reason || fs.next_action.tool || "")}</p>`
    : "";
  $("feature-body").innerHTML = rows
    ? `<table class="feature-table"><thead><tr><th>Item</th><th>Title</th><th>Phase</th><th></th></tr></thead><tbody>${rows}</tbody></table>${next}`
    : `<p class="muted small">No children captured yet.</p>${next}`;
  for (const btn of $("feature-body").querySelectorAll("[data-open]")) {
    btn.onclick = () => selectMission(btn.dataset.open);
  }
  for (const btn of $("feature-body").querySelectorAll("[data-launch]")) {
    btn.onclick = () => {
      $("launch-wi").value = btn.dataset.launch;
      $("launch-name").value = `wi${btn.dataset.launch}`;
      $("launch-name").dataset.touched = "1";
      toast("Launch Pad pre-filled — hit LAUNCH MISSION");
      window.scrollTo(0, 0);
    };
  }
}

/* ---------------- PR panel ---------------- */
function renderPrPanel(pulse) {
  const panel = $("pr-panel");
  const prPhases = new Set(["pr_prepared", "pr_created", "merged"]);
  if (!pulse.pr && !prPhases.has(pulse.progress.current)) { panel.hidden = true; return; }
  panel.hidden = false;
  $("pr-meta").innerHTML = pulse.pr
    ? `PR <b>#${esc(String(pulse.pr.pr_id))}</b> · ${esc(pulse.pr.source_branch)} → ${esc(pulse.pr.target_branch)}
       ${pulse.pr.url ? ` · <a href="${esc(pulse.pr.url)}" target="_blank" style="color:var(--accent-2)">open in ADO</a>` : ""}`
    : `<span class="muted">No PR record yet — ⏩ Advance creates it when the item is verified.</span>`;
}
async function prAction(path, btn) {
  btn.disabled = true;
  const out = $("pr-out");
  out.hidden = false;
  out.classList.remove("error-card");
  out.textContent = "…";
  try {
    const data = await api(`/api/item/${encodeURIComponent(state.selected)}${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const r = data.result || {};
    if (path === "/pr-status") {
      out.innerHTML = `<div class="ready-row">Status: <b>${esc(String(r.status || r.merge_status || "unknown"))}</b></div>`
        + (r.votes ? `<div class="ready-row">Votes: ${esc(JSON.stringify(r.votes))}</div>` : "")
        + (r.next_action ? `<div class="ready-row"><b>Next:</b> ${esc(String(r.next_action))}</div>` : "");
    } else {
      const threads = r.threads || r.comments || [];
      out.innerHTML = threads.length
        ? threads.slice(0, 20).map((t) => `<div class="ready-row">💬 <b>${esc(String(t.author || t.id || ""))}</b> ${esc(String(t.file || ""))}: ${esc(String(t.snippet || t.comment || t.text || "")).slice(0, 220)}</div>`).join("")
        : `<div class="ready-row">No open review threads 🎉</div>`;
    }
    refreshItem();
  } catch (e) {
    if (e.detail && (e.detail.reason || e.detail.hint)) {
      out.classList.add("error-card");
      out.innerHTML = `<div class="err-title">⚠ ${esc(e.detail.tool || "PR call")} blocked</div>
        <div class="err-msg">${esc(e.detail.error || "")}</div>
        ${e.detail.hint ? `<div class="err-row"><b>What to do:</b> ${esc(e.detail.hint)}</div>` : ""}`;
    } else out.textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- needs-you panel ---------------- */
function renderNeedsYou(pulse) {
  const body = $("needs-body");
  body.innerHTML = "";
  let count = 0;

  // 1) open clarifications — answerable only while the item is in the plan stage
  const open = pulse.clarifications.filter((q) => q.open);
  if (open.length && pulse.clarifications_locked) {
    const card = document.createElement("div");
    card.className = "need judgment";
    card.innerHTML = `
      <h3>Leftover planning question${open.length === 1 ? "" : "s"} (locked)</h3>
      <p class="muted small">This item is past planning (stage: <b>${esc(pulse.stage)}</b>), so the server
      policy locks clarification answers. The question${open.length === 1 ? "" : "s"} below
      ${open.length === 1 ? "is" : "are"} left over from planning and ${open.length === 1 ? "does not" : "do not"} block delivery.</p>
      ${open.map((q) => `<p><span class="q-id">${esc(q.id)}</span> <span class="muted">${esc(q.question)}</span></p>`).join("")}`;
    body.appendChild(card);
  } else if (open.length) {
    count += open.length;
    const card = document.createElement("div");
    card.className = "need";
    card.innerHTML = `<h3>Open questions — answer here or hit Auto-clarify</h3>`;
    for (const q of open) {
      const wrap = document.createElement("div");
      wrap.innerHTML = `
        <p><span class="q-id">${esc(q.id)}</span> ${esc(q.question)}</p>
        ${q.options.length ? `<p class="muted small">Options: ${esc(q.options.join(" | "))}</p>` : ""}
        <textarea data-qid="${esc(q.id)}" placeholder="Your answer…"></textarea>`;
      card.appendChild(wrap);
    }
    const actions = document.createElement("div");
    actions.className = "need-actions";
    const btn = document.createElement("button");
    btn.className = "btn ok";
    btn.textContent = "Submit answers";
    btn.onclick = () => submitAnswers(card);
    actions.appendChild(btn);
    card.appendChild(actions);
    body.appendChild(card);
  }

  // 2) pending approvals
  for (const a of pulse.approvals.filter((a) => a.status === "pending")) {
    count += 1;
    const card = document.createElement("div");
    card.className = "need";
    card.innerHTML = `
      <h3>Human gate — approve the <span class="q-id">${esc(a.phase)}</span> phase</h3>
      <p class="muted small">${esc(a.summary)}</p>
      <textarea data-feedback placeholder="Feedback (optional)…"></textarea>`;
    const actions = document.createElement("div");
    actions.className = "need-actions";
    for (const [label, verdict, cls] of [["✔ Approve", "approve", "ok"], ["✖ Reject", "reject", "danger"], ["↺ Request changes", "request_changes", ""]]) {
      const btn = document.createElement("button");
      btn.className = `btn ${cls}`;
      btn.textContent = label;
      btn.onclick = () => submitDecision(a.phase, verdict, card.querySelector("[data-feedback]").value);
      actions.appendChild(btn);
    }
    card.appendChild(actions);
    body.appendChild(card);
  }

  // 3) judgment step → hand off to the agent
  const current = pulse.progress.current;
  if (!count && JUDGMENT_PHASES.has(current)) {
    count += 1;
    const card = document.createElement("div");
    card.className = "need judgment";
    card.innerHTML = `
      <h3>Agent judgment step</h3>
      <p>${esc(JUDGMENT_HINTS[current] || "This step needs model-authored content.")}</p>
      <p class="muted small">Open the <b>bc-orchestrator</b> agent in VS Code and paste the prompt.</p>`;
    const actions = document.createElement("div");
    actions.className = "need-actions";
    const btn = document.createElement("button");
    btn.className = "btn";
    btn.textContent = "📋 Copy agent prompt";
    btn.onclick = copyAgentPrompt;
    actions.appendChild(btn);
    card.appendChild(actions);
    body.appendChild(card);
  }

  $("needs-you").hidden = body.children.length === 0;
  $("needs-count").textContent = count || "";
}

/* ---------------- artifacts + events ---------------- */
function renderArtifacts(artifacts) {
  const list = $("artifact-list");
  list.innerHTML = "";
  for (const f of artifacts) {
    const li = document.createElement("li");
    li.innerHTML = `<span>${f.review ? "★ " : ""}${esc(f.name)}</span>
      <span class="muted small">${(f.size / 1024).toFixed(1)} KB ${f.review ? '<span class="review-flag">REVIEW</span>' : ""}</span>`;
    li.onclick = () => openArtifact(f.name);
    list.appendChild(li);
  }
}
function renderEvents(events) {
  const log = $("event-log");
  log.innerHTML = "";
  for (const e of events) {
    const li = document.createElement("li");
    const ts = (e.ts || "").replace("T", " ").slice(5, 16);
    li.innerHTML = `<span class="event-kind ${esc(e.kind)}">${esc(e.kind)}</span>
      <span>${esc(e.summary)}</span><span class="event-ts">${esc(ts)}</span>`;
    log.appendChild(li);
  }
}
async function openArtifact(name) {
  try {
    const data = await api(`/api/item/${encodeURIComponent(state.selected)}/artifact?name=${encodeURIComponent(name)}`);
    if (data.error) return toast(data.error, true);
    $("viewer-title").textContent = `${state.selected} / ${name}`;
    $("viewer-body").innerHTML = name.endsWith(".md")
      ? renderMd(data.content)
      : `<pre>${esc(data.content)}</pre>`;
    $("viewer").hidden = false;
  } catch (e) { toast(e.message, true); }
}

/* ---------------- actions ---------------- */
async function post(path, body, btn) {
  if (btn) btn.disabled = true;
  try {
    const data = await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    showActionOutput(data.result ?? data);
    toast(`${data.tool || "action"} done`);
    refreshItem();
    refreshOverview();
    return data;
  } catch (e) {
    if (e.detail && (e.detail.reason || e.detail.hint || e.detail.tool)) showActionError(e.detail);
    else showActionOutput(e.message);
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function submitAnswers(card) {
  const answers = {};
  for (const ta of card.querySelectorAll("textarea[data-qid]")) {
    if (ta.value.trim()) answers[ta.dataset.qid] = ta.value.trim();
  }
  if (!Object.keys(answers).length) return toast("Fill at least one answer first", true);
  await post(`/api/item/${encodeURIComponent(state.selected)}/answer`, { answers });
}
async function submitDecision(phase, decision, feedback) {
  await post(`/api/item/${encodeURIComponent(state.selected)}/decision`, { phase, decision, feedback });
}
function copyAgentPrompt() {
  const spec = state.selected;
  const prompt =
    `Use the bc-orchestrator agent. Continue item "${spec}" ` +
    `(project root: ${state.projectRoot}). Call bc_status(spec_name="${spec}") and follow ` +
    `enforcement.next_actions strictly in order until the next human gate; produce any ` +
    `model-authored artifact through its bc_* tool only.`;
  navigator.clipboard.writeText(prompt).then(
    () => toast("Agent prompt copied — paste it into the bc-orchestrator chat"),
    () => { showActionOutput(prompt); toast("Copy blocked — prompt shown below", true); });
}

/* Action payload = presets + this mission's worktree as project_root (isolation). */
function actionBody() {
  const p = { ...loadPresets() };
  delete p.agent_cmd;
  delete p.worktrees_base;
  const wt = state.lastPulse && state.lastPulse.worktree;
  if (wt && wt.exists && wt.path) p.project_root = wt.path;
  return p;
}

/* ---------------- routines (user-defined schedules) ---------------- */
let routinesCache = [];
let routineActions = {};
function routineRow(r) {
  const opts = Object.entries(routineActions).map(([k, v]) =>
    `<option value="${esc(k)}" ${k === r.action ? "selected" : ""}>${esc(k)} — ${esc(v)}</option>`).join("");
  return `<div class="routine-row" data-id="${esc(r.id)}">
    <input class="r-name" type="text" value="${esc(r.name)}" placeholder="name" style="width:24%">
    <select class="r-action" style="width:30%">${opts}</select>
    <input class="r-time" type="text" value="${esc(r.time)}" placeholder="07:30" style="width:12%">
    <select class="r-days" style="width:15%">
      ${["daily", "weekdays", "weekend"].map((d) => `<option ${d === r.days ? "selected" : ""}>${d}</option>`).join("")}
    </select>
    <label class="radio" style="margin:0"><input class="r-enabled" type="checkbox" ${r.enabled ? "checked" : ""}> on</label>
    <button class="btn small-btn r-run" title="Run now">▶</button>
    <button class="btn small-btn danger r-del" title="Remove">✕</button>
  </div>`;
}
function renderRoutines() {
  $("routines-list").innerHTML = routinesCache.map(routineRow).join("")
    || `<p class="muted small">No routines yet — add one. Example: autopilot_sweep every weekday at 07:30.</p>`;
  for (const btn of $("routines-list").querySelectorAll(".r-del")) {
    btn.onclick = () => {
      routinesCache = routinesCache.filter((r) => r.id !== btn.closest(".routine-row").dataset.id);
      renderRoutines();
    };
  }
  for (const btn of $("routines-list").querySelectorAll(".r-run")) {
    btn.onclick = async () => {
      const id = btn.closest(".routine-row").dataset.id;
      btn.disabled = true;
      try {
        const data = await api(`/api/routines/${encodeURIComponent(id)}/run`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        const out = $("routines-out");
        out.hidden = false;
        out.textContent = `${data.record.routine}: ${data.record.summary}\n` + JSON.stringify(data.record.details || "", null, 2);
        loadRoutines();
      } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
    };
  }
}
function collectRoutines() {
  return Array.from($("routines-list").querySelectorAll(".routine-row")).map((row) => ({
    id: row.dataset.id,
    name: row.querySelector(".r-name").value.trim(),
    action: row.querySelector(".r-action").value,
    time: row.querySelector(".r-time").value.trim(),
    days: row.querySelector(".r-days").value,
    enabled: row.querySelector(".r-enabled").checked,
    last_run: (routinesCache.find((r) => r.id === row.dataset.id) || {}).last_run || "",
  }));
}
async function loadRoutines() {
  try {
    const data = await api("/api/routines");
    routinesCache = data.routines;
    routineActions = data.actions;
    renderRoutines();
    $("routines-runs").innerHTML = (data.runs || []).slice(0, 12).map((r) =>
      `<div class="ready-row">${r.ok ? "✅" : "❌"} <b>${esc(r.routine || r.action)}</b> ${esc(r.summary)} <span class="muted mono">${esc((r.ts || "").slice(5, 16).replace("T", " "))}</span></div>`).join("")
      || `<p class="muted small">no runs yet</p>`;
  } catch (e) { toast(e.message, true); }
}

/* ---------------- autopilot ---------------- */
function paintAutopilot() {
  const btn = $("btn-autopilot");
  btn.textContent = `\u{1F6F8} Autopilot: ${state.autopilot ? "ON" : "OFF"}`;
  btn.style.borderColor = state.autopilot ? "var(--ok)" : "";
  btn.style.color = state.autopilot ? "var(--ok)" : "";
}
async function runAutopilot() {
  if (!state.autopilot || state.autopilotBusy || !state.selected) return;
  const pulse = state.lastPulse;
  if (!pulse) return;
  // Human gates / judgment phases: autopilot waits — a notification already fired.
  const phase = pulse.progress.current;
  if (pulse.is_feature || phase === "archived") return;
  state.autopilotBusy = true;
  try {
    const data = await api(`/api/item/${encodeURIComponent(state.selected)}/advance`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(actionBody()),
    });
    const r = data.result || {};
    const stop = r.stop || r.status || "";
    showActionOutput(r);
    if (/waiting_human|waiting_judgment|waiting_input|blocked|error|done/.test(String(stop))) {
      toast(`Autopilot paused: ${String(stop).replaceAll("_", " ")}`);
      if (notifyEnabled()) new Notification(`\u{1F6F8} ${state.selected}`, { body: `Autopilot stopped: ${r.reason || stop}` });
    }
  } catch (e) {
    if (e.detail) showActionError(e.detail);
    toast(`Autopilot: ${e.message}`, true);
  } finally {
    state.autopilotBusy = false;
    refreshOverview();
  }
}

/* ---------------- refinement lab ---------------- */
function renderIntakePanel(pulse) {
  const panel = $("intake-panel");
  if (!pulse.is_intake) { panel.hidden = true; return; }
  panel.hidden = false;
  // Push-to-ADO: visible once graduated as a feature with recorded children.
  const grad = pulse.graduation;
  const canPush = grad && grad.lane === "feature" && (grad.children || []).length;
  $("push-ado-wrap").hidden = !canPush;
  if (canPush && !$("push-parent").value) $("push-parent").value = grad.work_item_id || "";
  const d = pulse.dossier;
  const lane = d && d.lane ? d.lane.suggested_lane : "";
  $("intake-lane").textContent = lane ? `suggested: ${lane}` : "not analyzed yet";
  if (!d) {
    $("intake-dossier").innerHTML =
      `<p class="muted small">No dossier yet — hit 🔎 Re-analyze or 🤖 Dispatch agent.</p>`;
    return;
  }
  const rows = [];
  rows.push(`<div class="ready-row"><b>Similar past work:</b></div>`);
  for (const p of d.precedents || []) {
    rows.push(`<div class="ready-row">• <b>${esc(p.spec)}</b> <span class="badge">${p.score}</span> <span class="muted small">${esc(p.preview).slice(0, 140)}…</span></div>`);
  }
  if (!(d.precedents || []).length) rows.push(`<div class="ready-row muted">• none found</div>`);
  rows.push(`<div class="ready-row" style="margin-top:6px"><b>Code reality:</b></div>`);
  for (const h of d.code_reality || []) {
    rows.push(`<div class="ready-row mono small">• ${esc(h.mention)} → ${esc(h.object)} <span class="muted">(${h.fields} fields)</span></div>`);
  }
  if (!(d.code_reality || []).length) rows.push(`<div class="ready-row muted">• no object mentions resolved</div>`);
  rows.push(`<div class="ready-row" style="margin-top:6px"><b>Open questions (answer before graduating):</b></div>`);
  for (const q of d.open_questions || []) {
    rows.push(`<div class="ready-row">• <span class="q-id">${esc(q.id)}</span> ${esc(q.question)}</div>`);
  }
  $("intake-dossier").innerHTML = rows.join("");
}

/* ---------------- bug diagnosis panel ---------------- */
function renderBugPanel(pulse) {
  const panel = $("bug-panel");
  if (pulse.lane !== "bugfix") { panel.hidden = true; return; }
  panel.hidden = false;
  const rc = pulse.root_cause;
  $("bug-badge").textContent = rc ? "diagnosis recorded" : "diagnosis required";
  $("bug-badge").className = rc ? "badge ok-badge" : "badge hot";
  $("bug-recorded").hidden = !rc;
  $("bug-form").hidden = !!rc;
  if (rc) {
    $("bug-recorded").innerHTML = `
      <div class="ready-row"><b>Symptom:</b> ${esc(rc.symptom)}</div>
      <div class="ready-row"><b>Root cause:</b> ${esc(rc.root_cause)}</div>
      <div class="ready-row"><b>Fix approach:</b> ${esc(rc.fix_approach)}</div>
      <div class="ready-row muted">✓ ${rc.evidence_count} evidence reference(s) verified against the repo
        — full write-up in <b>ROOT-CAUSE.md</b> (artifacts).</div>`;
  }
}

/* ---------------- guidance renderer ---------------- */
function renderGuidance(result) {
  const el = $("action-output");
  el.hidden = false;
  el.classList.remove("error-card");
  const enf = result.enforcement || {};
  const actions = enf.next_actions || [];
  if (enf.all_ok) {
    el.innerHTML = `<div class="ready-row ok-text">✅ All enforcement engines pass — nothing is blocking this item.
      Use ⏩ Advance for the next deterministic step, or the agent prompt for judgment work.</div>`;
    return;
  }
  if (!actions.length) {
    el.textContent = JSON.stringify(result, null, 2);
    return;
  }
  el.innerHTML = `<div class="err-title" style="color:var(--accent-2)">🛰 Do this next — in order:</div>`
    + actions.map((a, i) => `
      <div class="guide-step">
        <span class="guide-num">${i + 1}</span>
        <div>
          <div><b>${esc(a.tool || "")}</b> <span class="badge">${esc(a.engine || "")}</span></div>
          <div class="small">${esc(a.reason || "")}</div>
          ${a.params_hint ? `<div class="muted small mono">${esc(JSON.stringify(a.params_hint))}</div>` : ""}
        </div>
      </div>`).join("");
}

/* ---------------- wire up ---------------- */
$("intake-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("intake-name").value.trim();
  const files = Array.from($("intake-files").files || []).slice(0, 10);
  const docs = [];
  for (const f of files) {
    try { docs.push({ filename: f.name, content: await f.text() }); }
    catch { toast(`could not read ${f.name}`, true); }
  }
  const btn = $("intake-btn");
  btn.disabled = true;
  try {
    const data = await api("/api/intake/start", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, text: $("intake-text").value, docs }),
    });
    toast(`Intake analyzed — dossier ready`);
    $("intake-text").value = ""; $("intake-files").value = "";
    selectMission(data.intake);
  } catch (e) {
    if (e.detail && (e.detail.hint || e.detail.reason)) showActionError(e.detail);
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
});
document.querySelectorAll("input[name=grad-lane]").forEach((r) => {
  r.addEventListener("change", () => {
    $("grad-children-wrap").hidden = document.querySelector("input[name=grad-lane]:checked").value !== "feature";
  });
});
$("btn-intake-analyze").onclick = (e) => post(`/api/item/${encodeURIComponent(state.selected)}/intake-analyze`, {}, e.target);
$("btn-graduate").onclick = async (e) => {
  const lane = document.querySelector("input[name=grad-lane]:checked").value;
  const spec = $("grad-spec").value.trim();
  if (!spec) return toast("Give the new mission a name first", true);
  const body = { lane, spec_name: spec, work_item_id: $("grad-wi").value.trim(),
                 children: $("grad-children").value };
  const data = await post(`/api/item/${encodeURIComponent(state.selected)}/intake-graduate`, body, e.target);
  if (data) { toast(`Graduated → ${spec} (${lane})`); selectMission(spec); }
};

$("launch-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("launch-name").value.trim();
  const presets = loadPresets();
  const body = {
    work_item_id: $("launch-wi").value.trim(),
    tier: document.querySelector("input[name=tier]:checked").value,
    org_url: $("launch-org").value.trim() || presets.org_url || "",
    project: $("launch-project").value.trim() || presets.project || "",
  };
  const data = await post(`/api/item/${encodeURIComponent(name)}/capture`, body, $("launch-btn"));
  if (data) selectMission(name);
});
$("launch-wi").addEventListener("input", () => {
  const wi = $("launch-wi").value.trim();
  if (wi && !$("launch-name").dataset.touched) $("launch-name").value = wi ? `wi${wi}` : "";
});
$("launch-name").addEventListener("input", () => { $("launch-name").dataset.touched = "1"; });

$("btn-advance").onclick = (e) => post(`/api/item/${encodeURIComponent(state.selected)}/advance`, actionBody(), e.target);
$("btn-autopilot").onclick = () => {
  state.autopilot = !state.autopilot;
  paintAutopilot();
  toast(state.autopilot ? "Autopilot ON — advancing until a gate needs you" : "Autopilot OFF");
  if (state.autopilot) runAutopilot();
};
$("btn-run-tests").onclick = async (e) => {
  e.target.textContent = "\u{1F9EA} Running… (can take minutes)";
  await post(`/api/item/${encodeURIComponent(state.selected)}/run-tests`, actionBody(), e.target);
  e.target.textContent = "\u{1F9EA} Run tests";
};
$("btn-worktree").onclick = async (e) => {
  const wt = state.lastPulse && state.lastPulse.worktree;
  if (wt && wt.exists) {
    showActionOutput(`Worktree ready:\n${wt.path}\nbranch: ${wt.branch}\n\nAdvance / Run tests / Dispatch automatically use it.`);
    return;
  }
  const presets = loadPresets();
  await post(`/api/item/${encodeURIComponent(state.selected)}/worktree`,
    { action: "create", worktrees_base: presets.worktrees_base || "" }, e.target);
};
$("btn-dispatch").onclick = async (e) => {
  const presets = loadPresets();
  const body = presets.agent_cmd ? { template: presets.agent_cmd } : {};
  e.target.disabled = true;
  try {
    const data = await api(`/api/item/${encodeURIComponent(state.selected)}/dispatch`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    toast(`Agent started (pid ${data.pid}) — live log below`);
    pollDispatch();
  } catch (err) {
    if (err.detail && (err.detail.hint || err.detail.error)) showActionError(err.detail);
    else toast(err.message, true);
  } finally {
    e.target.disabled = false;
  }
};
async function pollDispatch() {
  if (!state.selected) return;
  const strip = $("dispatch-strip");
  try {
    const s = await api(`/api/item/${encodeURIComponent(state.selected)}/dispatch-status`);
    if (!s.active) { strip.hidden = true; return; }
    strip.hidden = false;
    strip.textContent =
      `\u{1F916} ${s.agent} ${s.running ? "RUNNING" : `finished (exit ${s.returncode})`} · ` +
      `${s.elapsed_s}s · ${s.cwd}\n\u2500\u2500 log tail \u2500\u2500\n${s.tail || "(no output yet)"}`;
    if (s.running) setTimeout(pollDispatch, 3000);
    else { toast(`Agent finished (exit ${s.returncode})`); refreshItem(); refreshOverview(); }
  } catch { strip.hidden = true; }
}
$("btn-preflight").onclick = (e) => post(`/api/env-preflight`, loadPresets(), e.target);
$("btn-metrics").onclick = (e) => post(`/api/item/${encodeURIComponent(state.selected)}/metrics`, {}, e.target);
$("btn-diff").onclick = async () => {
  try {
    const d = await api(`/api/item/${encodeURIComponent(state.selected)}/diff`);
    $("viewer-title").textContent = `${state.selected} — git diff (allowed files)`;
    const bodyText = (d.stat ? d.stat + "\n" : "") + (d.patch || "(no changes vs HEAD)")
      + (d.untracked ? "\n\nUntracked:\n" + d.untracked : "")
      + (d.truncated ? "\n\n[… diff truncated for viewer]" : "");
    $("viewer-body").innerHTML = `<pre>${esc(bodyText)}</pre>`;
    $("viewer").hidden = false;
  } catch (e) { toast(e.message, true); }
};
$("btn-prepare-review").onclick = async (e) => {
  const bullets = $("pg-bullets").value.trim();
  if (!bullets) return toast("Write the requirement bullets first", true);
  await post(`/api/item/${encodeURIComponent(state.selected)}/prepare-review`, { human_bullets: bullets }, e.target);
};
$("btn-request-approval").onclick = async (e) => {
  const summary = $("pg-summary").value.trim();
  if (!summary) return toast("Write a one-line approval summary first", true);
  await post(`/api/item/${encodeURIComponent(state.selected)}/request-approval`, { summary }, e.target);
};
$("btn-consistency").onclick = (e) => post(`/api/item/${encodeURIComponent(state.selected)}/consistency`, {}, e.target);
$("btn-push-ado").onclick = async (e) => {
  if (!$("push-confirm").checked) return toast("Tick the confirmation box first — this writes to ADO", true);
  const presets = loadPresets();
  await post(`/api/item/${encodeURIComponent(state.selected)}/push-items`, {
    parent_work_item_id: $("push-parent").value.trim(),
    org_url: presets.org_url || "", project: presets.project || "",
    confirm: true,
  }, e.target);
};
$("btn-routines").onclick = () => { $("routines-modal").hidden = false; loadRoutines(); };
$("routines-close").onclick = () => { $("routines-modal").hidden = true; };
$("routines-modal").addEventListener("click", (ev) => { if (ev.target === $("routines-modal")) $("routines-modal").hidden = true; });
$("routine-add").onclick = () => {
  routinesCache.push({ id: Math.random().toString(36).slice(2, 10), name: "",
    action: "pr_sweep", time: "07:30", days: "weekdays", enabled: true, last_run: "" });
  renderRoutines();
};
$("routines-save").onclick = async (e) => {
  e.target.disabled = true;
  try {
    const data = await api("/api/routines", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ routines: collectRoutines() }) });
    routinesCache = data.routines;
    renderRoutines();
    toast(`Schedule saved — ${data.saved} routine(s)`);
  } catch (err) {
    if (err.detail && err.detail.hint) showActionError(err.detail);
    toast(err.message, true);
  } finally { e.target.disabled = false; }
};
$("btn-tool-health").onclick = async (e) => {
  e.target.disabled = true;
  try {
    const data = await api("/api/tool-health", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const r = data.result || {};
    const out = $("routines-out");
    out.hidden = false;
    const cands = r.improvement_candidates || [];
    out.textContent = `analyzed ${r.entries_analyzed || 0} audit entries — ${cands.length} improvement candidate(s)\n`
      + cands.map((c) => `• ${c.tool}: ${Math.round(c.failure_rate * 100)}% of ${c.calls} calls fail`).join("\n")
      + `\n\nfull report: policy/tool_health.md`;
  } catch (err) { toast(err.message, true); } finally { e.target.disabled = false; }
};
$("btn-root-cause").onclick = async (e) => {
  const body = {
    symptom: $("bf-symptom").value.trim(),
    root_cause: $("bf-cause").value.trim(),
    fix_approach: $("bf-fix").value.trim(),
    evidence: $("bf-evidence").value,
    regression_risk: $("bf-risk").value.trim(),
  };
  await post(`/api/item/${encodeURIComponent(state.selected)}/root-cause`, body, e.target);
};
$("btn-autoclarify").onclick = (e) => post(`/api/item/${encodeURIComponent(state.selected)}/auto-clarify`, {}, e.target);
$("btn-guidance").onclick = async (e) => {
  e.target.disabled = true;
  try {
    const data = await api(`/api/item/${encodeURIComponent(state.selected)}/guidance`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    renderGuidance(data.result || {});
  } catch (err) {
    if (err.detail && (err.detail.reason || err.detail.hint)) showActionError(err.detail);
    else showActionOutput(err.message);
  } finally {
    e.target.disabled = false;
  }
};
$("btn-agent-prompt").onclick = copyAgentPrompt;
$("btn-presets").onclick = openPresets;
$("presets-close").onclick = () => { $("presets-modal").hidden = true; };
$("presets-save").onclick = savePresetsFromForm;
$("presets-modal").addEventListener("click", (ev) => { if (ev.target === $("presets-modal")) $("presets-modal").hidden = true; });
$("btn-notify").onclick = toggleNotify;
$("btn-pr-status").onclick = (e) => prAction("/pr-status", e.target);
$("btn-pr-comments").onclick = (e) => prAction("/pr-comments", e.target);
$("viewer-close").onclick = () => { $("viewer").hidden = true; };
$("viewer").addEventListener("click", (ev) => { if (ev.target === $("viewer")) $("viewer").hidden = true; });
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { $("viewer").hidden = true; $("presets-modal").hidden = true; $("routines-modal").hidden = true; } });

paintNotifyBtn();
refreshOverview();
state.overviewTimer = setInterval(refreshOverview, POLL_OVERVIEW_MS);
