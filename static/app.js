/* Hermes Monitor — app.js
   Vanilla JS, no frameworks. Uses EventSource for SSE, fetch for API.
*/

// ── Constants ──────────────────────────────────────────────────────────────

const AGENT_IDS = ["sage", "imagine", "ink", "recon", "signal"];

const AGENT_META = {
  sage:    { emoji: "🧭", color: "#58a6ff", label: "SAGE" },
  imagine: { emoji: "🎨", color: "#bc8cff", label: "IMAGINE" },
  ink:     { emoji: "🖊️", color: "#3fb950", label: "INK" },
  recon:   { emoji: "🔎", color: "#f0883e", label: "RECON" },
  signal:  { emoji: "📡", color: "#79c0ff", label: "SIGNAL" },
};

const KIND_ICON = {
  tool_call:      "🔧",
  user_message:   "📨",
  response:       "💬",
  delegation:     "🎯",
  file_write:     "📄",
  tool_result:    "✅",
  tool_error:     "❌",
  subagent_result:"📬",
};

const IMAGE_EXTS = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"]);

const MAX_FEED_EVENTS = 500;
const MAX_LOG_LINES   = 400;

// ── State ──────────────────────────────────────────────────────────────────

let currentLogSource = "gateway";
let feedCount = 0;
let atBottom = true;
let feedFilter = "all";
const feedRowsByCallId = new Map();
let latestToolActivityRows = [];
let promptTraces = [];

const agentState = {};
AGENT_IDS.forEach(id => {
  agentState[id] = { active: false, last_seen: null, session: null, lastAction: "" };
});

const logBuffers = { gateway: [], imagine: [] };

// Kanban — keep the latest board snapshot client-side so card list re-renders
// cheaply when SSE pushes a card update.
let kanbanBoard = { tasks: [], counts: {}, available: false };

// File registry (newest first in UI)
const fileRegistry = [];
let fileCount = 0;

// SSE reconnect
let sseSource = null;
let reconnectDelay = 3000;
let reconnectTimer = null;

// ── Health / Issues ────────────────────────────────────────────────────────────
// issues is a Map of issue_id → issue dict, mirroring server issues_state.
const issues = new Map();

function handleHealthInit(data) {
  issues.clear();
  (data.issues || []).forEach(iss => issues.set(iss.id, iss));
  renderIssues();
}

function handleHealthAlert(data) {
  if (data.action === "raise") {
    issues.set(data.issue.id, data.issue);
  } else if (data.action === "resolve") {
    issues.delete(data.issue_id);
  }
  renderIssues();
}

const SEV_ICON  = { critical: "🔴", warning: "🟡", info: "🔵" };
const SEV_ORDER = { critical: 0, warning: 1, info: 2 };

function renderIssues() {
  const panel     = document.getElementById("health-panel");
  const bar       = document.getElementById("health-bar");
  const issuesEl  = document.getElementById("health-issues");
  const countEl   = document.getElementById("health-issue-count");
  if (!panel || !bar || !issuesEl) return;

  const list = [...issues.values()].sort(
    (a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9)
  );
  const hasCritical = list.some(i => i.severity === "critical");
  const hasWarning  = list.some(i => i.severity === "warning");

  panel.className = list.length === 0 ? "health-clean"
                  : hasCritical       ? "health-critical"
                  : "health-warning";

  const okIcon = bar.querySelector(".health-ok-icon");
  const okText = bar.querySelector(".health-ok-text");

  if (list.length === 0) {
    if (okIcon) okIcon.style.display = "";
    if (okText) okText.style.display = "";
    countEl.style.display = "none";
    issuesEl.style.display = "none";
    issuesEl.innerHTML = "";
    return;
  }

  if (okIcon) okIcon.style.display = "none";
  if (okText) okText.style.display = "none";
  countEl.textContent = `${list.length} issue${list.length > 1 ? "s" : ""}`;
  countEl.style.display = "";
  issuesEl.style.display = "";

  issuesEl.innerHTML = list.map(iss => {
    const icon   = SEV_ICON[iss.severity] || "⚪";
    const agentMeta = iss.agent ? (AGENT_META[iss.agent] || null) : null;
    const agentBadge = agentMeta
      ? `<span class="issue-agent-badge" style="color:${agentMeta.color}">${agentMeta.emoji} ${escHtml(agentMeta.label)}</span>`
      : (iss.agent ? `<span class="issue-agent-badge">${escHtml(iss.agent)}</span>` : "");
    const ts = iss.ts ? `<span class="issue-ts">${escHtml(formatDisplayTime(iss.ts))}</span>` : "";
    const detail = iss.detail
      ? `<div class="issue-detail">${escHtml(iss.detail)}</div>` : "";
    return `
      <div class="health-issue sev-${escHtml(iss.severity)}" data-id="${escHtml(iss.id)}">
        <span class="issue-sev-badge">${icon}</span>
        <div class="issue-body">
          <div class="issue-title">${escHtml(iss.title)}</div>
          ${detail}
        </div>
        ${agentBadge}
        ${ts}
      </div>`;
  }).join("")
;}

// ── Init ───────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  buildAgentCards();
  setupCollapsiblePanels();
  updateNavbarClock();
  setInterval(updateNavbarClock, 1000);
  loadInitialFiles();
  setInterval(loadInitialFiles, 15000);
  fetchKanbanBoard();
  connectSSE();
  fetchStatus();
  fetchBackupStatus();
  fetchUsage();
  fetchActivity();
  fetchPromptTraces();
  setupFeedScroll();
  setInterval(fetchBackupStatus, 30000);
  // Token usage refreshes more often — it changes with every API call.
  setInterval(fetchUsage, 10000);
  setInterval(fetchActivity, 5000);
  setInterval(fetchPromptTraces, 10000);
});

async function fetchUsage() {
  try {
    const res = await fetch("/api/usage");
    const data = await res.json();
    renderUsage(data);
  } catch (e) {
    // server hiccup — leave previous render in place
  }
}

function fmtNum(n) {
  return (n || 0).toLocaleString();
}

function fmtMoney(n) {
  if (n == null || Number.isNaN(Number(n))) return "n/a";
  const v = Number(n);
  if (v === 0) return "$0.0000";
  if (Math.abs(v) < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

function fmtShortTime(ts) {
  return formatDisplayTime(ts);
}

function renderUsage(data) {
  const active = data.active || {};
  const history = data.history || [];
  const ork = data.openrouter || {};
  const providers = data.providers || {};
  const openrouter = providers.openrouter || {};
  const xUsage = providers.x || {};
  const tavily = providers.tavily || {};

  // Credit pill (top-right of the panel header)
  const creditEl = document.getElementById("usage-credit");
  if (creditEl) {
    if (ork && typeof ork.usage === "number" && typeof ork.limit === "number") {
      const remaining = (ork.limit - ork.usage);
      creditEl.textContent = `$${remaining.toFixed(2)} left`;
      creditEl.title = `Used $${ork.usage.toFixed(2)} of $${ork.limit.toFixed(2)} on OpenRouter`;
    } else if (ork.error) {
      creditEl.textContent = "—";
      creditEl.title = `OpenRouter: ${ork.error}`;
    } else {
      creditEl.textContent = "—";
    }
  }

  renderProviderUsageCards(openrouter, xUsage, tavily);

  // Active prompt rows — one per agent that has had recent activity
  const activeEl = document.getElementById("usage-active");
  if (activeEl) {
    const ids = Object.keys(active).sort();
    if (!ids.length) {
      activeEl.innerHTML = `<div class="usage-empty">No prompts tracked yet</div>`;
    } else {
      activeEl.innerHTML = ids.map(aid => {
        const a = active[aid];
        const meta = AGENT_META[aid] || { emoji: "•", label: aid.toUpperCase(), color: "#999" };
        const cacheRatio = a.cache_reads
          ? Math.round(100 * a.cache_hits / a.cache_reads)
          : null;
        return `
          <div class="usage-row" style="--agent-color:${meta.color}">
            <div class="usage-row-head">
              <span class="usage-agent">${meta.emoji} ${escHtml(meta.label)}</span>
              <span class="usage-calls">${a.calls} call${a.calls === 1 ? "" : "s"} · ${fmtMoney(a.estimated_cost_usd || 0)}</span>
            </div>
            <div class="usage-msg" title="${escHtml(a.msg || "")}">${escHtml(a.msg || "")}</div>
            <div class="usage-counts">
              <span title="input tokens">↑ ${fmtNum(a.input)}</span>
              <span title="output tokens">↓ ${fmtNum(a.output)}</span>
              ${cacheRatio !== null ? `<span title="cache hit ratio">cache ${cacheRatio}%</span>` : ""}
            </div>
          </div>
        `;
      }).join("");
    }
  }

  // History (last N prompts) — newest first
  const histEl = document.getElementById("usage-history");
  if (histEl) {
    const items = [...history].reverse().slice(0, 10);
    if (!items.length) {
      histEl.innerHTML = `<div class="usage-empty">(empty)</div>`;
    } else {
      histEl.innerHTML = items.map(h => {
        const meta = AGENT_META[h.agent] || { emoji: "•", label: (h.agent || "?").toUpperCase() };
        return `
          <div class="usage-hist-row">
            <span class="usage-hist-tag">${meta.emoji}</span>
            <span class="usage-hist-msg">${escHtml((h.msg || "").slice(0, 60))}</span>
            <span class="usage-hist-counts">↑${fmtNum(h.input)} ↓${fmtNum(h.output)} · ${h.calls}c · ${fmtMoney(h.estimated_cost_usd || 0)}</span>
          </div>
        `;
      }).join("");
    }
  }
}

function renderProviderUsageCards(openrouter, xUsage, tavily) {
  const cardsEl = document.getElementById("usage-provider-cards");
  if (!cardsEl) return;
  const recentLine = (rows, fallback) => {
    const first = (rows || [])[0];
    if (!first) return fallback;
    const label = first.query || first.detail || first.model || first.kind || fallback;
    return `${fmtShortTime(first.ts)} · ${label}`.slice(0, 120);
  };
  const modelBits = (openrouter.models || []).slice(0, 2).map(m =>
    `<span>${escHtml(m.model || "model")}: ${fmtMoney(m.estimated_cost || 0)}</span>`
  ).join("");
  cardsEl.innerHTML = `
    <div class="usage-provider-card provider-openrouter">
      <div class="usage-provider-head"><span>OpenRouter</span><strong>${fmtMoney(openrouter.estimated_cost_today || 0)}</strong></div>
      <div class="usage-provider-sub">${fmtNum(openrouter.calls_today)} calls · ↑${fmtNum(openrouter.input_today)} ↓${fmtNum(openrouter.output_today)} · cache ${fmtNum(openrouter.cache_read_today)}</div>
      <div class="usage-provider-models">${modelBits || "<span>No model spend today</span>"}</div>
    </div>
    <div class="usage-provider-card provider-x">
      <div class="usage-provider-head"><span>X API</span><strong>${fmtNum(xUsage.calls_today)} calls</strong></div>
      <div class="usage-provider-sub">${fmtNum(xUsage.usage_units_today)} usage units · ${fmtNum(xUsage.failures_today)} failures</div>
      <div class="usage-provider-recent">${escHtml(recentLine(xUsage.recent, "No X searches recorded"))}</div>
    </div>
    <div class="usage-provider-card provider-tavily">
      <div class="usage-provider-head"><span>Tavily</span><strong>${fmtNum(tavily.calls_today)} calls</strong></div>
      <div class="usage-provider-sub">${fmtNum(tavily.usage_units_today)} usage units · ${fmtNum(tavily.failures_today)} failures</div>
      <div class="usage-provider-recent">${escHtml(recentLine(tavily.recent, "No Tavily calls recorded"))}</div>
    </div>
  `;
}

async function fetchPromptTraces() {
  try {
    const res = await fetch("/api/prompt-traces");
    const data = await res.json();
    promptTraces = data.traces || [];
    renderPromptTraces();
  } catch (e) {
    // keep previous render
  }
}

async function fetchActivity() {
  try {
    const res = await fetch("/api/activity?limit=80");
    const data = await res.json();
    renderActivity(data);
  } catch (e) {
    const status = document.getElementById("activity-status");
    if (status) status.textContent = "offline";
  }
}

function activityToolLabel(tool) {
  if (!tool) return "tool";
  if (tool === "read_file") return "read";
  if (tool.startsWith("memory:")) return tool.replace("memory:", "vector.");
  if (tool === "x:search") return "x.search";
  if (tool === "tavily:search") return "tavily.search";
  if (tool === "tavily:extract") return "tavily.extract";
  return tool;
}

function activityToolClass(tool) {
  if (tool === "read_file") return "read";
  if (tool === "memory:search") return "memory-search";
  if (tool === "memory:store") return "memory-store";
  if (tool === "memory:ingest") return "memory-ingest";
  if (tool === "x:search") return "x-search";
  if (tool === "tavily:search") return "tavily-search";
  if (tool === "tavily:extract") return "tavily-extract";
  return "tool";
}

function formatJsonish(value) {
  if (value == null || value === "") return "";
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch (e) {
      return value;
    }
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch (e) {
    return String(value);
  }
}

function renderActivity(data) {
  const listEl = document.getElementById("activity-list");
  const statusEl = document.getElementById("activity-status");
  const summaryEl = document.getElementById("activity-summary");
  if (!listEl) return;

  if (data.error) {
    if (statusEl) statusEl.textContent = "error";
    listEl.innerHTML = `<div class="activity-empty">${escHtml(data.error)}</div>`;
    if (summaryEl) summaryEl.innerHTML = "";
    return;
  }

  const rows = data.rows || [];
  latestToolActivityRows = rows;
  if (statusEl) statusEl.textContent = `${rows.length}`;

  const counts = data.counts || {};
  if (summaryEl) {
    const bits = Object.keys(counts).sort().map(k =>
      `<span class="activity-chip chip-${escHtml(activityToolClass(k))}">${escHtml(activityToolLabel(k))}: ${counts[k]}</span>`
    );
    summaryEl.innerHTML = bits.join("");
  }

  if (!rows.length) {
    listEl.innerHTML = `<div class="activity-empty">No memory or file activity yet</div>`;
    return;
  }

  listEl.innerHTML = rows.map(row => {
    const idx = rows.indexOf(row);
    const agent = row.agent || "unknown";
    const meta = AGENT_META[agent] || { emoji: "•", label: agent.toUpperCase(), color: "#8b949e" };
    const toolClass = activityToolClass(row.tool);
    const detail = row.detail || "";
    const extra = row.extra || {};
    const result = extra.result || extra.result_preview || "";
    const duration = Number.isFinite(row.duration_ms) && row.duration_ms > 0
      ? `<span title="duration">${row.duration_ms}ms</span>` : "";
    return `
      <div class="activity-row tool-${escHtml(toolClass)} clickable" data-activity-index="${idx}" style="--agent-color:${meta.color}" title="${escHtml(detail)}">
        <div class="activity-row-head">
          <span class="activity-agent">${meta.emoji} ${escHtml(meta.label)}</span>
          <span class="activity-time">${escHtml(formatAmPm(row.ts || ""))}</span>
        </div>
        <div class="activity-tool">${escHtml(activityToolLabel(row.tool))}</div>
        <div class="activity-detail">${escHtml(detail)}</div>
        <div class="activity-meta">
          ${duration}
          ${row.session_id ? `<span title="session">${escHtml(row.session_id.slice(0, 12))}</span>` : ""}
          <span class="activity-open-hint">${result ? "result" : "details"}</span>
        </div>
      </div>
    `;
  }).join("");

  listEl.querySelectorAll(".activity-row[data-activity-index]").forEach(el => {
    el.addEventListener("click", () => {
      const row = latestToolActivityRows[Number(el.dataset.activityIndex)];
      if (row) openActivityViewer(row, el);
    });
  });
}

function openActivityViewer(row, rowEl) {
  const extra = row.extra || {};
  const query = extra.cmd || extra.path || row.detail || "";
  const result = extra.result || extra.result_preview || "";
  const meta = Object.fromEntries(Object.entries(extra).filter(([k]) => !["result", "result_preview"].includes(k)));
  const isX = row.tool === "x:search";
  const isTavily = row.tool && row.tool.startsWith("tavily:");
  const header = isX ? "X Search Context" : isTavily ? "Tavily Context" : (row.tool === "read_file" ? "File / Query" : "Query / Command");
  const full = [
    `## ${header}\n${query || "(none)"}`,
    (isX || isTavily) && meta.args ? `## Parameters\n${formatJsonish(meta.args)}` : "",
    `## Result Preview\n${formatJsonish(result) || "(no result captured for this row)"}`,
    Object.keys(meta).length ? `## Metadata\n${formatJsonish(meta)}` : "",
  ].filter(Boolean).join("\n\n");
  openLogViewer({
    agent: row.agent || "unknown",
    kind: row.tool === "read_file" ? "read_file" : "tool_call",
    title: activityToolLabel(row.tool),
    detail: row.detail || "",
    full,
    ts: row.ts || "",
  }, rowEl);
}

// ── Agent Cards ────────────────────────────────────────────────────────────

function buildAgentCards() {
  const container = document.getElementById("agent-cards");
  container.innerHTML = "";
  AGENT_IDS.forEach(id => {
    const meta = AGENT_META[id];
    const card = document.createElement("div");
    card.className = "agent-card";
    card.id = `card-${id}`;
    card.style.setProperty("--agent-color", meta.color);
    card.innerHTML = `
      <div class="agent-header">
        <div class="agent-dot" id="dot-${id}"></div>
        <span class="agent-emoji">${meta.emoji}</span>
        <span class="agent-name">${meta.label}</span>
        <span class="agent-controls" id="controls-${id}">
          <button class="agent-ctrl start"   onclick="agentLifecycle('${id}','start')"   title="Start gateway">▶</button>
          <button class="agent-ctrl stop"    onclick="agentLifecycle('${id}','stop')"    title="Stop gateway">⏸</button>
          <button class="agent-ctrl restart" onclick="agentLifecycle('${id}','restart')" title="Restart gateway">↻</button>
        </span>
      </div>
      <div class="agent-meta">
        <span id="seen-${id}">Never active</span>
        <span class="agent-session" id="session-${id}"></span>
      </div>
      <div class="agent-action" id="action-${id}"></div>
    `;
    container.appendChild(card);
  });
}

async function agentLifecycle(agentId, action) {
  // Disable the buttons during the round-trip so impatient double-clicks
  // don't double-fire launchctl.
  const wrap = document.getElementById(`controls-${agentId}`);
  if (wrap) wrap.classList.add("busy");
  try {
    const res = await fetch(`/api/agent/${encodeURIComponent(agentId)}/${action}`, { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      if (data.kind === "no_gateway") {
        // Pure worker — show a tooltip via alert, but don't error out.
        alert(`${agentId} is a pure Kanban worker — there's no always-on gateway to ${action}. The dispatcher spawns it on demand when a card arrives.`);
      } else {
        alert(`${action} failed: ${data.error || data.message || "unknown"}`);
      }
      return;
    }
    if (data.noop) console.info(`agent ${agentId}: ${data.message}`);
  } catch (e) {
    alert(`${action} request failed: ${e.message}`);
  } finally {
    if (wrap) wrap.classList.remove("busy");
    // Re-poll status so the green dot reflects reality
    setTimeout(fetchStatus, 700);
  }
}

function updateAgentCard(agentId, data) {
  const meta = AGENT_META[agentId];
  if (!meta) return;

  const state = agentState[agentId] || {};
  Object.assign(state, data);
  agentState[agentId] = state;

  const card    = document.getElementById(`card-${agentId}`);
  const dot     = document.getElementById(`dot-${agentId}`);
  const seen    = document.getElementById(`seen-${agentId}`);
  const session = document.getElementById(`session-${agentId}`);
  const action  = document.getElementById(`action-${agentId}`);
  if (!card) return;

  const isActive = state.active;
  card.classList.toggle("active", isActive);
  dot.classList.toggle("active", isActive);

  if (state.last_seen) {
    const timePart = formatDisplayTime(state.last_seen);
    seen.textContent = isActive ? `Active · ${timePart}` : `Last seen ${timePart}`;
  }

  if (state.session) {
    session.textContent = `session ${state.session}`;
  }

  if (state.lastAction) {
    action.textContent = state.lastAction;
  }

  // Reflect gateway state on the controls. Pure workers (no_gateway) get
  // all three buttons disabled with a tooltip.
  const controls = document.getElementById(`controls-${agentId}`);
  if (controls) {
    const hasGateway = state.has_gateway !== false; // assume true if field missing
    const running    = !!state.gateway_running;
    controls.classList.toggle("no-gateway", !hasGateway);
    controls.classList.toggle("running", running);
    const btns = {
      start:   controls.querySelector(".start"),
      stop:    controls.querySelector(".stop"),
      restart: controls.querySelector(".restart"),
    };
    if (!hasGateway) {
      Object.values(btns).forEach(b => {
        if (!b) return;
        b.disabled = true;
        b.title = "Pure Kanban worker — no gateway. Spawned on demand by Sage's dispatcher.";
      });
    } else {
      btns.start && (btns.start.disabled = running, btns.start.title = running ? "Gateway already running" : "Start gateway");
      btns.stop  && (btns.stop.disabled  = !running, btns.stop.title  = running ? "Stop gateway"           : "Gateway already stopped");
      btns.restart && (btns.restart.disabled = !running, btns.restart.title = running ? "Restart gateway"  : "Gateway is stopped");
    }
  }
}

// ── SSE Connection ─────────────────────────────────────────────────────────

function connectSSE() {
  if (sseSource) {
    sseSource.close();
    sseSource = null;
  }
  clearTimeout(reconnectTimer);

  showConnStatus("Connecting…", "");

  sseSource = new EventSource("/api/events");

  sseSource.onopen = () => {
    reconnectDelay = 3000;
    setMonitorConnectionState("online");
    showConnStatus("Connected", "ok");
    setTimeout(() => hideConnStatus(), 2000);
  };

  sseSource.onmessage = (e) => {
    try {
      const ev = JSON.parse(e.data);
      handleSSEEvent(ev);
    } catch (err) {
      // ignore parse errors
    }
  };

  sseSource.onerror = () => {
    sseSource.close();
    sseSource = null;
    setMonitorConnectionState("offline");
    showConnStatus(`Reconnecting in ${Math.round(reconnectDelay / 1000)}s…`, "error");
    reconnectTimer = setTimeout(() => {
      reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
      connectSSE();
    }, reconnectDelay);
  };
}

function handleSSEEvent(ev) {
  switch (ev.type) {
    case "kanban_board":
      kanbanBoard = ev;
      renderKanbanBoard();
      break;

    case "kanban_card_update":
      // Merge or insert the updated card and re-render
      mergeKanbanCard(ev.card);
      renderKanbanBoard();
      // Also pulse the card in the activity feed for visibility
      appendKanbanFeedEntry(ev.card, ev.prev_status);
      break;

    case "agent_status":
      updateAgentCard(ev.agent, {
        active: ev.active,
        last_seen: ev.last_seen,
        session: ev.session,
      });
      break;

    case "agent_event":
      if (ev.kind === "tool_result" && ev.call_id && attachToolResult(ev)) {
        updateAgentCard(ev.agent, { lastAction: ev.title });
        break;
      }
      appendFeedEvent(ev);
      updateAgentCard(ev.agent, { lastAction: ev.title });
      break;

    case "log_line":
      appendLogLine(ev.source, ev.level, ev.text);
      break;

    case "file_event":
      addFileToRegistry(ev);
      break;

    case "health_init":
      handleHealthInit(ev);
      break;

    case "health_alert":
      handleHealthAlert(ev);
      break;

    case "usage_update":
      renderUsage(ev);
      break;

    case "prompt_trace_init":
      promptTraces = ev.traces || [];
      renderPromptTraces();
      break;

    case "prompt_trace_update":
      upsertPromptTrace(ev.trace);
      renderPromptTraces();
      break;

    case "heartbeat":
    case "backlog_begin":
    case "backlog_end":
      // alive / control markers
      break;
  }
}

function upsertPromptTrace(trace) {
  if (!trace || !trace.id) return;
  const idx = promptTraces.findIndex(t => t.id === trace.id);
  if (idx >= 0) promptTraces[idx] = trace;
  else promptTraces.unshift(trace);
  promptTraces.sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  promptTraces = promptTraces.slice(0, 30);
}

function renderPromptTraces() {
  const list = document.getElementById("prompt-trace-list");
  const count = document.getElementById("prompt-trace-count");
  if (!list) return;
  if (count) count.textContent = `${promptTraces.length}`;
  if (!promptTraces.length) {
    list.innerHTML = `<div class="prompt-trace-empty">No Slack prompt traces yet</div>`;
    return;
  }
  list.innerHTML = promptTraces.slice(0, 8).map((trace, idx) => {
    const usage = trace.usage || {};
    const elapsed = trace.ended_at && trace.started_at ? `${Math.round(trace.ended_at - trace.started_at)}s` : "running";
    const status = trace.status || "running";
    const events = (trace.events || []).slice(-4).map(e =>
      `<span>${escHtml(e.kind || "event")}: ${escHtml((e.detail || "").slice(0, 48))}</span>`
    ).join("");
    return `
      <div class="prompt-trace-row status-${escHtml(status)} clickable" data-trace-index="${idx}">
        <div class="prompt-trace-head">
          <span>${escHtml((trace.platform || "slack").toUpperCase())}</span>
          <span>${escHtml(elapsed)} · ${fmtMoney(usage.cost || 0)}</span>
        </div>
        <div class="prompt-trace-msg">${escHtml(trace.msg || "")}</div>
        <div class="prompt-trace-meta">calls ${fmtNum(usage.calls)} · ↑${fmtNum(usage.input)} ↓${fmtNum(usage.output)}</div>
        <div class="prompt-trace-events">${events || "<span>waiting for activity</span>"}</div>
      </div>
    `;
  }).join("");
  list.querySelectorAll(".prompt-trace-row[data-trace-index]").forEach(el => {
    el.addEventListener("click", () => {
      const trace = promptTraces[Number(el.dataset.traceIndex)];
      if (trace) openPromptTraceViewer(trace, el);
    });
  });
}

function openPromptTraceViewer(trace, rowEl) {
  const usage = trace.usage || {};
  const events = (trace.events || []).map(e => {
    const when = e.ts ? fmtShortTime(e.ts) : "";
    return `- ${when} ${e.kind}: ${e.detail || ""}`;
  }).join("\n");
  const full = [
    `## Slack Request\n${trace.msg || "(none)"}`,
    `## Summary\nstatus: ${trace.status || "running"}\nelapsed: ${trace.ended_at && trace.started_at ? Math.round(trace.ended_at - trace.started_at) + "s" : "running"}\nmodel calls: ${usage.calls || 0}\ntokens: ${usage.input || 0} input / ${usage.output || 0} output\nestimated cost: ${fmtMoney(usage.cost || 0)}`,
    `## Timeline\n${events || "(no events yet)"}`,
    trace.final ? `## Final Response\n${trace.final}` : "",
  ].filter(Boolean).join("\n\n");
  openLogViewer({
    agent: trace.agent || "sage",
    kind: "prompt_trace",
    title: "Prompt Trace",
    detail: trace.msg || "",
    full,
    ts: trace.started_at ? fmtShortTime(trace.started_at) : "",
  }, rowEl);
}

// ── Kanban board ───────────────────────────────────────────────────────────

async function fetchKanbanBoard() {
  try {
    const res = await fetch("/api/kanban");
    if (!res.ok) return;
    kanbanBoard = await res.json();
    renderKanbanBoard();
  } catch (e) {
    // ignore
  }
}

function mergeKanbanCard(card) {
  const tasks = kanbanBoard.tasks || [];
  const i = tasks.findIndex(t => t.id === card.id);
  if (i >= 0) tasks[i] = card;
  else tasks.unshift(card);
  kanbanBoard.tasks = tasks;
}

const KANBAN_STATUS_COLOR = {
  ready:     "#9aa0a6",
  todo:      "#9aa0a6",
  triage:    "#9aa0a6",
  running:   "#f0b429",
  blocked:   "#e94560",
  scheduled: "#7c8b9c",
  done:      "#3fb950",
  archived:  "#4d5566",
};

function renderKanbanBoard() {
  const activeEl = document.getElementById("kanban-active");
  const doneEl   = document.getElementById("kanban-done");
  if (!activeEl || !doneEl) return;
  const tasks = kanbanBoard.tasks || [];
  const active = tasks.filter(t => t.status !== "done" && t.status !== "archived");
  const done   = tasks.filter(t => t.status === "done");
  renderKanbanCol(activeEl, active, "No active cards");
  renderKanbanCol(doneEl, done, "No completed cards");
  renderKanbanPills();
}

function renderKanbanCol(el, list, emptyText) {
  if (!list.length) {
    el.innerHTML = `<div class="kanban-empty">${emptyText}</div>`;
    return;
  }
  el.innerHTML = list.map(t => {
    const meta = AGENT_META[t.assignee] || { emoji: "•", color: "#9aa0a6", label: (t.assignee || "?").toUpperCase() };
    const statusColor = KANBAN_STATUS_COLOR[t.status] || "#9aa0a6";
    const elapsed = t.elapsed_s != null ? `${humanDuration(t.elapsed_s)}` : "";
    return `
      <div class="kanban-card" data-id="${t.id}" onclick="openCardDrawer('${t.id}')">
        <div class="kanban-card-row1">
          <span class="kanban-card-status" style="background:${statusColor}"></span>
          <span class="kanban-card-assignee" style="color:${meta.color}">${meta.emoji} ${meta.label}</span>
          <span class="kanban-card-elapsed">${elapsed}</span>
          ${t.status !== "done" ? `<button class="kanban-card-cancel" title="Cancel &amp; remove task (reclaim + archive)"
                  onclick="event.stopPropagation(); cancelCard('${t.id}')">⊘</button>` : ""}
          <button class="kanban-card-archive" title="Archive card"
                  onclick="event.stopPropagation(); archiveCard('${t.id}')">✕</button>
        </div>
        <div class="kanban-card-title">${escHtml(t.title || "")}</div>
        <div class="kanban-card-id">${t.id} · ${t.status}</div>
      </div>
    `;
  }).join("");
}

async function cancelCard(taskId) {
  if (!taskId) return;
  try {
    const res = await fetch(`/api/kanban/card/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      console.warn("cancel failed", taskId, data.error);
      return;
    }
    kanbanBoard.tasks = (kanbanBoard.tasks || []).filter(t => t.id !== taskId);
    renderKanbanBoard();
    renderKanbanPills();
  } catch (e) {
    console.warn("cancel request failed", e);
  }
}

async function archiveCard(taskId) {
  if (!taskId) return;
  try {
    const res = await fetch(`/api/kanban/card/${encodeURIComponent(taskId)}/archive`, { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      console.warn("archive failed", taskId, data.error);
      return;
    }
    // Remove locally — the server will also broadcast a kanban_board update.
    kanbanBoard.tasks = (kanbanBoard.tasks || []).filter(t => t.id !== taskId);
    renderKanbanBoard();
    renderKanbanPills();
  } catch (e) {
    console.warn("archive request failed", e);
  }
}

async function archiveAllDone() {
  const doneCount = (kanbanBoard.tasks || []).filter(t => t.status === "done").length;
  if (!doneCount) return;
  if (!confirm(`Archive ${doneCount} completed card${doneCount === 1 ? "" : "s"}? They stay in kanban.db but disappear from this view.`)) return;
  try {
    const res = await fetch("/api/kanban/archive-done", { method: "POST" });
    const data = await res.json();
    if (!data.ok) {
      alert("Archive failed: " + (data.error || "unknown"));
      return;
    }
    kanbanBoard.tasks = (kanbanBoard.tasks || []).filter(t => t.status !== "done");
    renderKanbanBoard();
    renderKanbanPills();
  } catch (e) {
    alert("Archive request failed: " + e.message);
  }
}

function renderKanbanPills() {
  const counts = kanbanBoard.counts || {};
  const ready   = (counts.ready || 0) + (counts.todo || 0) + (counts.triage || 0);
  const running = counts.running || 0;
  const blocked = (counts.blocked || 0) + (counts.scheduled || 0);
  const done    = counts.done || 0;
  const pillsEl = document.getElementById("kanban-pills");
  if (!pillsEl) return;
  pillsEl.innerHTML = `
    <span class="pill pill-ready"   title="ready / todo / triage">${ready} ready</span>
    <span class="pill pill-running" title="running">${running} running</span>
    <span class="pill pill-blocked" title="blocked / scheduled">${blocked} blocked</span>
    <span class="pill pill-done"    title="done (lifetime)">${done} done</span>
  `;
}

function appendKanbanFeedEntry(card, prevStatus) {
  const meta = AGENT_META[card.assignee] || { emoji: "•", label: (card.assignee || "?").toUpperCase() };
  const transition = prevStatus ? `${prevStatus} → ${card.status}` : card.status;
  appendFeedEvent({
    type: "agent_event",
    agent: card.assignee || "sage",
    ts: "",
    kind: "delegation",
    title: `[${card.id}] ${meta.emoji} ${meta.label} · ${transition}`,
    detail: card.title,
    full: `Click the card on the board to see the full handoff.`,
  });
}

// ── Card drawer ────────────────────────────────────────────────────────────

async function openCardDrawer(taskId) {
  const drawer = document.getElementById("card-drawer");
  drawer.classList.add("open");
  document.getElementById("card-drawer-id").textContent = taskId;
  document.getElementById("card-drawer-status").textContent = "loading…";
  document.getElementById("card-drawer-body").innerHTML = `<div class="card-drawer-loading">Loading…</div>`;
  try {
    const res = await fetch(`/api/kanban/card/${encodeURIComponent(taskId)}`);
    const data = await res.json();
    if (data.error) {
      document.getElementById("card-drawer-body").innerHTML = `<div class="card-drawer-error">${escHtml(data.error)}</div>`;
      return;
    }
    renderCardDrawer(data);
  } catch (e) {
    document.getElementById("card-drawer-body").innerHTML = `<div class="card-drawer-error">Request failed: ${escHtml(e.message)}</div>`;
  }
}

function closeCardDrawer() {
  document.getElementById("card-drawer").classList.remove("open");
}

function renderCardDrawer(data) {
  const t = data.task || {};
  const meta = AGENT_META[t.assignee] || { emoji: "•", label: (t.assignee || "?").toUpperCase(), color: "#9aa0a6" };
  document.getElementById("card-drawer-id").innerHTML =
    `<span style="color:${meta.color}">${meta.emoji} ${meta.label}</span> · ${t.id}`;
  document.getElementById("card-drawer-status").innerHTML =
    `<span class="card-status-pill" style="background:${KANBAN_STATUS_COLOR[t.status] || "#9aa0a6"}">${t.status}</span>`;

  const created = t.created_at ? formatUnix(t.created_at) : "?";
  const started = t.started_at ? formatUnix(t.started_at) : "—";
  const completed = t.completed_at ? formatUnix(t.completed_at) : "—";
  const elapsed = (t.started_at && t.completed_at)
    ? humanDuration(t.completed_at - t.started_at)
    : (t.started_at ? humanDuration(Math.floor(Date.now()/1000) - t.started_at) + " · running" : "—");

  const events = (data.events || []).map(ev => {
    const ts = formatUnix(ev.created_at);
    const payload = ev.payload && Object.keys(ev.payload).length
      ? `<pre class="card-event-payload">${escHtml(JSON.stringify(ev.payload, null, 2))}</pre>`
      : "";
    return `
      <div class="card-event">
        <div class="card-event-head"><span class="card-event-kind">${ev.kind}</span><span class="card-event-ts">${ts}</span></div>
        ${payload}
      </div>
    `;
  }).join("");

  const runs = (data.runs || []).map(r => {
    const meta = r.metadata || {};
    const metaPretty = Object.keys(meta).length
      ? `<pre class="card-event-payload">${escHtml(JSON.stringify(meta, null, 2))}</pre>` : "";
    const imgPath = meta.image_path;
    const imgPreview = imgPath
      ? `<a href="/api/file?path=${encodeURIComponent(imgPath)}" target="_blank"><img class="card-image-preview" src="/api/file?path=${encodeURIComponent(imgPath)}" alt="output"/></a>`
      : "";
    return `
      <div class="card-run">
        <div class="card-event-head">
          <span class="card-event-kind">run #${r.id} · ${r.profile || "?"}</span>
          <span class="card-event-ts">${r.outcome || r.status}</span>
        </div>
        ${r.summary ? `<div class="card-run-summary">${escHtml(r.summary)}</div>` : ""}
        ${imgPreview}
        ${metaPretty}
        ${r.error ? `<div class="card-run-error">${escHtml(r.error)}</div>` : ""}
      </div>
    `;
  }).join("");

  const comments = (data.comments || []).map(c =>
    `<div class="card-comment">
       <div class="card-comment-head">${escHtml(c.author || "?")} · ${formatUnix(c.created_at)}</div>
       <div class="card-comment-body">${escHtml(c.body || "")}</div>
     </div>`
  ).join("") || `<div class="card-empty">No comments</div>`;

  const logTail = (data.log_tail || []).map(escHtml).join("\n");
  const linksHtml = `
    ${data.parents && data.parents.length ? `<div>Parents: ${data.parents.join(", ")}</div>` : ""}
    ${data.children && data.children.length ? `<div>Children: ${data.children.join(", ")}</div>` : ""}
  `;

  document.getElementById("card-drawer-body").innerHTML = `
    <div class="card-section">
      <div class="card-title-line">${escHtml(t.title || "")}</div>
      ${linksHtml}
      <div class="card-timeline">
        <div><b>created</b>   ${created}</div>
        <div><b>started</b>   ${started}</div>
        <div><b>completed</b> ${completed}</div>
        <div><b>elapsed</b>   ${elapsed}</div>
        <div><b>workspace</b> <code>${escHtml(t.workspace_kind || "?")} ${t.workspace_path ? `· ${escHtml(t.workspace_path)}` : ""}</code></div>
      </div>
    </div>
    ${t.body ? `<div class="card-section"><div class="card-section-title">Brief</div><pre class="card-body">${escHtml(t.body)}</pre></div>` : ""}
    <div class="card-section">
      <div class="card-section-title">Runs (${data.runs ? data.runs.length : 0})</div>
      ${runs || `<div class="card-empty">No runs yet</div>`}
    </div>
    <div class="card-section">
      <div class="card-section-title">Events (${data.events ? data.events.length : 0})</div>
      ${events || `<div class="card-empty">No events</div>`}
    </div>
    <div class="card-section">
      <div class="card-section-title">Comments</div>
      ${comments}
    </div>
    ${logTail ? `<div class="card-section"><div class="card-section-title">Worker log (tail)</div><pre class="card-log">${logTail}</pre></div>` : ""}
  `;
}

// ── Time helpers ───────────────────────────────────────────────────────────

const LOCAL_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "local";

function parseTimestamp(value) {
  if (value == null || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    const ms = value < 1e12 ? value * 1000 : value;
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }
  const raw = String(value).trim();
  if (!raw) return null;
  if (/^\d+(\.\d+)?$/.test(raw)) {
    return parseTimestamp(Number(raw));
  }
  let normalized = raw;
  if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(raw)) {
    normalized = raw.replace(" ", "T");
  }
  const d = new Date(normalized);
  return Number.isNaN(d.getTime()) ? null : d;
}

function formatDisplayTime(value, opts = {}) {
  const d = parseTimestamp(value);
  if (!d) return value ? String(value) : "—";
  const options = {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    ...(opts.seconds ? { second: "2-digit" } : {}),
    ...(opts.date ? { month: "short", day: "numeric" } : {}),
    ...(opts.tz ? { timeZoneName: "short" } : {}),
  };
  return new Intl.DateTimeFormat("en-US", options).format(d);
}

function formatUnix(epoch) {
  if (!epoch) return "—";
  return formatDisplayTime(epoch, { date: true, tz: true });
}

function humanDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

async function fetchStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    if (data.agents) {
      Object.entries(data.agents).forEach(([id, s]) => {
        updateAgentCard(id, s);
      });
    }
    if (data.kanban && data.kanban.counts) {
      kanbanBoard.counts = data.kanban.counts;
      renderKanbanPills();
    }
  } catch (e) {
    // server not ready yet
  }
}

// ── Activity Feed ──────────────────────────────────────────────────────────

// Stack semantics: newest event at the TOP. `atTop` tracks whether the user is
// near the top — if yes, we keep snapping to the new latest. If they've
// scrolled down to inspect history, we leave their scroll position alone.
let atTop = true;

function setupFeedScroll() {
  const list = document.getElementById("feed-list");
  list.addEventListener("scroll", () => {
    const threshold = 40;
    atTop = list.scrollTop < threshold;
  });
}

function formatAmPm(ts) {
  return formatDisplayTime(ts || Date.now(), { tz: false });
}

function updateNavbarClock() {
  const el = document.getElementById("navbar-time");
  if (!el) return;
  el.textContent = formatDisplayTime(Date.now(), { seconds: true, tz: true });
  el.title = LOCAL_TZ;
}

function setMonitorConnectionState(state) {
  const badge = document.getElementById("navbar-connection");
  const label = document.getElementById("navbar-connection-label");
  if (!badge || !label) return;
  badge.classList.remove("state-online", "state-offline", "state-unknown");
  badge.classList.add(`state-${state}`);
  label.textContent = state === "online" ? "online" : state === "offline" ? "offline" : "?";
}

function setupCollapsiblePanels() {
  document.querySelectorAll(".panel-collapse-btn[data-collapse-target]").forEach(btn => {
    const targetId = btn.dataset.collapseTarget;
    const panel = document.getElementById(targetId);
    if (!panel) return;
    const key = `hermes-monitor:${targetId}:collapsed`;
    const apply = (collapsed) => {
      panel.classList.toggle("panel-collapsed", collapsed);
      btn.textContent = collapsed ? "⌄" : "⌃";
      btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
    };
    apply(localStorage.getItem(key) === "1");
    btn.addEventListener("click", () => {
      const collapsed = !panel.classList.contains("panel-collapsed");
      localStorage.setItem(key, collapsed ? "1" : "0");
      apply(collapsed);
    });
  });
}

function appendFeedEvent(ev) {
  const list = document.getElementById("feed-list");
  const empty = document.getElementById("feed-empty");
  if (empty) empty.style.display = "none";

  const meta = AGENT_META[ev.agent] || { label: ev.agent.toUpperCase(), color: "#8b949e" };
  const icon = KIND_ICON[ev.kind] || "•";
  const timeLabel = formatAmPm(ev.ts);

  const row = document.createElement("div");
  row.className = `event-row kind-${ev.kind}`;
  row.dataset.feedCategory = feedCategory(ev);
  row.__feedEvent = ev;
  if (!feedEventVisible(ev)) row.style.display = "none";

  // file_write events open the file viewer; other events with full text open a log viewer
  const isFileEvent = ev.kind === "file_write" && ev.detail;
  if (isFileEvent) {
    row.classList.add("clickable");
    row.onclick = () => openFileViewer(ev.detail, ev.agent);
    row.title = `Click to view: ${ev.detail}`;
  } else if (ev.full) {
    row.classList.add("clickable");
    row.onclick = () => openLogViewer(row.__feedEvent, row);
  }

  row.innerHTML = `
    <span class="event-ts">${escHtml(timeLabel)}</span>
    <span class="agent-badge badge-${ev.agent}">${escHtml(meta.label)}</span>
    <span class="event-icon">${icon}</span>
    <span class="event-title" title="${escHtml(ev.detail || ev.title || '')}">${escHtml(ev.title || '')}</span>
    <span class="event-result-pill" style="display:none">result</span>
  `;
  if (ev.result_full) {
    const pill = row.querySelector(".event-result-pill");
    if (pill) pill.style.display = "";
  }

  // Stack: newest first. Insert at the very top (above the .feed-empty if
  // it's still there, but we already hid it).
  if (list.firstChild) {
    list.insertBefore(row, list.firstChild);
  } else {
    list.appendChild(row);
  }

  // Trim from the BOTTOM now — oldest events fall off.
  const rows = list.querySelectorAll(".event-row");
  if (rows.length > MAX_FEED_EVENTS) rows[rows.length - 1].remove();

  feedCount++;
  document.getElementById("feed-count").textContent =
    `${feedCount} event${feedCount !== 1 ? "s" : ""}`;

  // If the user is near the top, keep them pinned to the newest item.
  if (atTop) list.scrollTop = 0;

  if (ev.call_id) {
    feedRowsByCallId.set(ev.call_id, row);
  }
}

function feedCategory(ev) {
  const text = `${ev.kind || ""} ${ev.title || ""} ${ev.detail || ""}`.toLowerCase();
  if ((ev.kind || "").includes("error") || text.includes("error") || text.includes("failed")) return "errors";
  if (text.includes("model #") || text.includes("api call")) return "model";
  if (text.includes("memory") || text.includes("vector")) return "memory";
  if (ev.kind === "delegation" || text.includes("kanban")) return "kanban";
  if (ev.kind === "tool_call" || ev.kind === "tool_result") return "tools";
  return "all";
}

function feedEventVisible(ev) {
  if (feedFilter === "all") return true;
  return feedCategory(ev) === feedFilter;
}

function setFeedFilter(value) {
  feedFilter = value || "all";
  document.querySelectorAll("#feed-list .event-row").forEach(row => {
    row.style.display = feedFilter === "all" || row.dataset.feedCategory === feedFilter ? "" : "none";
  });
}

function attachToolResult(resultEv) {
  const row = feedRowsByCallId.get(resultEv.call_id);
  if (!row) return false;
  const ev = row.__feedEvent || {};
  ev.result_title = resultEv.title || "";
  ev.result_detail = resultEv.detail || "";
  ev.result_full = resultEv.full || "";
  ev.result_ts = resultEv.ts || "";
  ev.result_kind = resultEv.kind || "tool_result";
  row.__feedEvent = ev;
  row.classList.add(resultEv.kind === "tool_error" ? "has-tool-error" : "has-tool-result");
  row.classList.add("clickable");
  row.onclick = () => openLogViewer(row.__feedEvent, row);
  const pill = row.querySelector(".event-result-pill");
  if (pill) {
    pill.textContent = resultEv.kind === "tool_error" ? "error" : "result";
    pill.style.display = "";
  }
  const title = row.querySelector(".event-title");
  if (title && resultEv.detail) {
    title.title = `${ev.detail || ev.title || ""}\n\nResult: ${resultEv.detail}`;
  }
  return true;
}

function clearFeed() {
  const list = document.getElementById("feed-list");
  list.querySelectorAll(".event-row").forEach(r => r.remove());
  feedRowsByCallId.clear();
  feedCount = 0;
  document.getElementById("feed-count").textContent = "0 events";
  const empty = document.getElementById("feed-empty");
  if (empty) empty.style.display = "";
}

// ── Files Panel ────────────────────────────────────────────────────────────

async function loadInitialFiles() {
  try {
    const res = await fetch("/api/files");
    const data = await res.json();
    syncFilesFromServer(data.files || []);
  } catch (e) {
    // ignore
  }
}

function syncFilesFromServer(files) {
  fileRegistry.length = 0;
  const sorted = [...files].sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  sorted.forEach(f => fileRegistry.push(f));
  fileCount = fileRegistry.length;
  renderFilesList();
}

function renderFilesList() {
  const list = document.getElementById("files-list");
  const countEl = document.getElementById("files-count");
  const empty = document.getElementById("files-empty");
  if (!list) return;

  list.querySelectorAll(".file-entry").forEach(r => r.remove());
  if (countEl) countEl.textContent = String(fileCount);

  if (fileCount === 0) {
    if (empty) empty.style.display = "";
    return;
  }
  if (empty) empty.style.display = "none";
  fileRegistry.forEach(f => list.appendChild(buildFileEntry(f, false)));
}

function addFileToRegistry(file, animate = true) {
  const existing = fileRegistry.find(f => f.path === file.path);
  if (existing) {
    Object.assign(existing, file);
    fileRegistry.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
    renderFilesList();
    return;
  }

  fileRegistry.unshift(file);
  fileRegistry.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  fileCount = fileRegistry.length;

  const countEl = document.getElementById("files-count");
  if (countEl) countEl.textContent = String(fileCount);

  const empty = document.getElementById("files-empty");
  if (empty) empty.style.display = "none";

  const list = document.getElementById("files-list");
  const entry = buildFileEntry(file, animate);
  list.insertBefore(entry, list.firstChild);
}

function buildFileEntry(file, animate = true) {
  const ext = (file.ext || "").toLowerCase();
  const isImage    = IMAGE_EXTS.has(ext);
  const isMarkdown = ext === ".md";
  const isHtml     = ext === ".html" || ext === ".htm";
  const icon = isImage ? "🖼️" : (isMarkdown ? "📝" : (isHtml ? "🌐" : "📄"));

  const sizeStr = formatBytes(file.size || 0);
  const timeStr = (file.ts || "").split(" ")[1] || file.ts || "";

  const entry = document.createElement("div");
  entry.className = "file-entry";
  if (animate) entry.style.animation = "fadeIn 0.2s ease";
  entry.onclick = () => openFileViewer(file.path, file.agent);
  entry.title = file.path;

  const cardChip = file.card_id
    ? `<span class="file-card-chip" title="open producing card" onclick="event.stopPropagation(); openCardDrawer('${file.card_id}')">↳ ${file.card_id}</span>`
    : "";

  entry.innerHTML = `
    <span class="file-icon">${icon}</span>
    <div class="file-info">
      <div class="file-name">${escHtml(file.filename || "")}</div>
      <div class="file-meta">
        <span class="agent-badge badge-${file.agent}" style="font-size:9px;padding:1px 4px">${(AGENT_META[file.agent] || {label: file.agent}).label}</span>
        ${escHtml(sizeStr)} · ${escHtml(timeStr)}
        ${cardChip}
      </div>
    </div>
  `;
  return entry;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

// ── File Viewer Modal ──────────────────────────────────────────────────────

function openFileViewer(path, agent) {
  const modal = document.getElementById("file-modal");
  const body  = document.getElementById("modal-body");
  const fname = document.getElementById("modal-filename");
  const badge = document.getElementById("modal-badge");
  const meta  = document.getElementById("modal-meta");
  const dl    = document.getElementById("modal-download");

  const filename = path.split("/").pop();
  const ext = ("." + filename.split(".").pop()).toLowerCase();
  const isImage    = IMAGE_EXTS.has(ext);
  const isMarkdown = ext === ".md";
  const isHtml     = ext === ".html" || ext === ".htm";
  const modalBox   = modal.querySelector(".modal-box");

  fname.textContent = filename;
  badge.className = `agent-badge badge-${agent}`;
  badge.textContent = (AGENT_META[agent] || { label: agent }).label;
  meta.textContent = "";
  dl.href = `/api/file?path=${encodeURIComponent(path)}`;
  dl.download = filename;
  if (modalBox) modalBox.classList.toggle("modal-box-html", isHtml);

  body.innerHTML = '<div class="modal-loading">Loading…</div>';
  modal.classList.add("open");

  const url = `/api/file?path=${encodeURIComponent(path)}`;

  if (isImage) {
    const img = document.createElement("img");
    img.className = "modal-image";
    img.onload = () => {
      meta.textContent = `${img.naturalWidth}×${img.naturalHeight}`;
      body.innerHTML = "";
      body.appendChild(img);
    };
    img.onerror = () => {
      body.innerHTML = '<div class="modal-error">Failed to load image</div>';
    };
    img.src = url;
  } else if (isHtml) {
    const iframe = document.createElement("iframe");
    iframe.className = "modal-html-frame";
    iframe.title = filename;
    iframe.sandbox = "allow-same-origin allow-popups allow-popups-to-escape-sandbox";
    iframe.onload = () => {
      meta.textContent = "rendered HTML";
    };
    iframe.onerror = () => {
      body.innerHTML = '<div class="modal-error">Failed to load HTML</div>';
    };
    body.innerHTML = "";
    body.appendChild(iframe);
    iframe.src = url;
  } else {
    fetch(url)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.text();
      })
      .then(text => {
        const byteLen = new TextEncoder().encode(text).length;
        const lines   = text.split("\n").length;

        if (isMarkdown && typeof marked !== "undefined") {
          meta.textContent = `${lines} lines · ${formatBytes(byteLen)} · rendered`;
          const div = document.createElement("div");
          div.className = "modal-markdown";
          div.innerHTML = marked.parse(text, { breaks: false, gfm: true });
          // Open links in new tab safely
          div.querySelectorAll("a").forEach(a => {
            a.target = "_blank";
            a.rel = "noopener noreferrer";
          });
          body.innerHTML = "";
          body.appendChild(div);
        } else {
          meta.textContent = `${lines} lines · ${formatBytes(byteLen)}`;
          const pre = document.createElement("pre");
          pre.className = "modal-text";
          pre.textContent = text;
          body.innerHTML = "";
          body.appendChild(pre);
        }
      })
      .catch(err => {
        body.innerHTML = `<div class="modal-error">Error: ${escHtml(err.message)}</div>`;
      });
  }
}

function openLogViewer(ev, rowEl) {
  // Remove any existing popover
  const existing = document.getElementById("log-popover");
  if (existing) { existing.remove(); return; }

  const agentMeta = AGENT_META[ev.agent] || { label: ev.agent.toUpperCase() };
  const kindLabel = (ev.kind || "").replace(/_/g, " ");
  const text = ev.full || "";
  const isMarkdown = ev.kind === "response" || ev.kind === "delegation" || ev.kind === "subagent_result";

  const pop = document.createElement("div");
  pop.id = "log-popover";
  pop.className = "log-popover";

  const hdr = document.createElement("div");
  hdr.className = "log-popover-header";
  hdr.innerHTML = `<span class="agent-badge badge-${ev.agent}">${escHtml(agentMeta.label)}</span><span class="log-popover-kind">${escHtml(kindLabel)}</span><span class="log-popover-ts">${escHtml(formatDisplayTime(ev.ts || Date.now(), { tz: true }))}</span><button class="log-popover-close" onclick="document.getElementById('log-popover').remove()">×</button>`;

  const body = document.createElement("div");
  body.className = "log-popover-body";

  if (ev.result_full) {
    const callSection = document.createElement("div");
    callSection.className = "log-popover-section";
    callSection.innerHTML = `<div class="log-popover-section-title">Tool Call</div>`;
    const callPre = document.createElement("pre");
    callPre.className = "log-popover-pre";
    callPre.textContent = formatJsonish(text || "");
    callSection.appendChild(callPre);
    body.appendChild(callSection);

    const resultSection = document.createElement("div");
    resultSection.className = "log-popover-section";
    resultSection.innerHTML = `<div class="log-popover-section-title">${ev.result_kind === "tool_error" ? "Tool Error" : "Tool Result"}</div>`;
    const resultPre = document.createElement("pre");
    resultPre.className = "log-popover-pre";
    resultPre.textContent = formatJsonish(ev.result_full || "");
    resultSection.appendChild(resultPre);
    body.appendChild(resultSection);
  } else if (isMarkdown && typeof marked !== "undefined") {
    const div = document.createElement("div");
    div.className = "modal-markdown";
    div.innerHTML = marked.parse(text, { breaks: false, gfm: true });
    div.querySelectorAll("a").forEach(a => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    body.appendChild(div);
  } else {
    const pre = document.createElement("pre");
    pre.className = "log-popover-pre";
    pre.textContent = text;
    body.appendChild(pre);
  }

  pop.appendChild(hdr);
  pop.appendChild(body);
  document.body.appendChild(pop);

  // Position: above the row, or below if not enough space
  const rect = rowEl.getBoundingClientRect();
  const popH = 280;
  const popW = Math.min(520, window.innerWidth - 32);
  let top = rect.top - popH - 6;
  if (top < 10) top = rect.bottom + 6;
  let left = rect.left;
  if (left + popW > window.innerWidth - 12) left = window.innerWidth - popW - 12;
  pop.style.top  = `${top}px`;
  pop.style.left = `${left}px`;
  pop.style.width = `${popW}px`;

  // Dismiss on outside click
  setTimeout(() => {
    document.addEventListener("click", function dismiss(e) {
      if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener("click", dismiss); }
    });
  }, 0);
}

function closeFileViewer(event) {
  if (event && event.target !== document.getElementById("file-modal")) return;
  const modal = document.getElementById("file-modal");
  modal.classList.remove("open");
  document.getElementById("modal-body").innerHTML = "";
  const modalBox = modal.querySelector(".modal-box");
  if (modalBox) modalBox.classList.remove("modal-box-html");
}

// ── Logs ───────────────────────────────────────────────────────────────────

async function loadInitialLogs(source) {
  try {
    const res = await fetch(`/api/logs?source=${source}&lines=200`);
    const data = await res.json();
    if (data.lines && data.lines.length > 0) {
      data.lines.forEach(line => {
        const level = detectLevel(line);
        logBuffers[source].push({ level, text: line });
      });
      if (source === currentLogSource) renderLogs();
    }
  } catch (e) {
    // ignore
  }
}

function formatLogLine(text) {
  // Highlight: timestamp, log level tag, agent name prefix (e.g. "sage:")
  let s = escHtml(text);
  // timestamp [HH:MM:SS] or YYYY-MM-DDTHH:MM:SS
  s = s.replace(/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})/g,
    '<span class="log-ts">$1</span>');
  s = s.replace(/(\[\d{2}:\d{2}:\d{2}\])/g,
    '<span class="log-ts">$1</span>');
  // level tags
  s = s.replace(/\[(INFO\s*)\]/g,    '<span class="log-lvl-info">[$1]</span>');
  s = s.replace(/\[(DEBUG\s*)\]/g,   '<span class="log-lvl-debug">[$1]</span>');
  s = s.replace(/\[(WARNING\s*)\]/g, '<span class="log-lvl-warn">[$1]</span>');
  s = s.replace(/\[(ERROR\s*)\]/g,   '<span class="log-lvl-error">[$1]</span>');
  // agent name prefix e.g. "sage:" or "imagine:"
  s = s.replace(/\b(sage|imagine):/g,
    '<span class="log-agent">$1:</span>');
  // ✅ ⚠ emoji highlights
  s = s.replace(/(✅)/g, '<span class="log-ok">$1</span>');
  s = s.replace(/(⚠|warning)/gi, '<span class="log-lvl-warn">$1</span>');
  return s;
}

function appendLogLine(source, level, text) {
  const buf = logBuffers[source] || (logBuffers[source] = []);
  buf.push({ level, text });
  if (buf.length > MAX_LOG_LINES) buf.shift();

  if (source === currentLogSource) {
    renderLogLine(level, text);
  }
}

function switchLog(source) {
  currentLogSource = source;
  document.querySelectorAll(".log-tab").forEach(t => t.classList.remove("active"));
  document.getElementById(`tab-${source}`).classList.add("active");
  renderLogs();
}

function renderLogs() {
  const container = document.getElementById("log-content");
  const buf = logBuffers[currentLogSource] || [];

  if (buf.length === 0) {
    container.innerHTML = '<div class="log-empty">No log entries yet</div>';
    return;
  }

  container.innerHTML = "";
  buf.forEach(({ level, text }) => {
    const el = document.createElement("div");
    el.className = `log-line ${level}`;
    el.innerHTML = formatLogLine(text);
    container.appendChild(el);
  });

  container.scrollTop = container.scrollHeight;
}

function renderLogLine(level, text) {
  const container = document.getElementById("log-content");

  const empty = container.querySelector(".log-empty");
  if (empty) empty.remove();

  const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 60;

  const el = document.createElement("div");
  el.className = `log-line ${level}`;
  el.innerHTML = formatLogLine(text);
  container.appendChild(el);

  const lines = container.querySelectorAll(".log-line");
  if (lines.length > MAX_LOG_LINES) lines[0].remove();

  if (wasAtBottom) container.scrollTop = container.scrollHeight;
}

function detectLevel(line) {
  if (line.includes("[DEBUG") || line.includes("[DEBUG]"))  return "DEBUG";
  if (line.includes("[WARN")  || line.includes("[WARNING")) return "WARNING";
  if (line.includes("[ERROR"))                               return "ERROR";
  if (line.includes("[INFO"))                                return "INFO";
  return "INFO";
}

// ── Connection status toast ────────────────────────────────────────────────

function showConnStatus(msg, cls) {
  const el = document.getElementById("conn-status");
  el.textContent = msg;
  el.className = `visible ${cls}`;
}

function hideConnStatus() {
  document.getElementById("conn-status").className = "";
}

// ── Backup ─────────────────────────────────────────────────────────────────

let backupRunning = false;

async function fetchBackupStatus() {
  try {
    const res = await fetch("/api/backup/status");
    const data = await res.json();
    updateBackupPanel(data);
  } catch (e) {
    const commit = document.getElementById("backup-commit");
    if (commit) commit.textContent = "Unavailable";
  }
}

function updateBackupPanel(data) {
  const commit = document.getElementById("backup-commit");
  const date   = document.getElementById("backup-date");
  const badge  = document.getElementById("backup-dirty-badge");
  const repoEl = document.getElementById("backup-repo");
  const btn    = document.getElementById("btn-backup");

  if (repoEl) {
    if (data.repo) {
      repoEl.textContent = data.repo;
      repoEl.title = data.repo;
      repoEl.style.display = "";
    } else {
      repoEl.textContent = "";
      repoEl.style.display = "none";
    }
  }

  if (btn) {
    btn.disabled = data.configured === false;
    btn.title = data.configured === false
      ? "Clone sf_agents to ~/Documents/sf_agents or set HERMES_BACKUP_REPO"
      : "Run backup.sh: ~/.hermes → sf_agents git commit";
  }

  if (!data.ok) {
    if (commit) commit.textContent = data.error || "Unavailable";
    if (date) date.textContent = "";
    if (badge) badge.style.display = "none";
    return;
  }
  if (data.last_commit) {
    const h = data.last_commit.hash ? `${data.last_commit.hash} · ` : "";
    if (commit) commit.textContent = h + (data.last_commit.message || "No commits yet");
    if (date)   date.textContent   = data.last_commit.date || "";
  } else {
    if (commit) commit.textContent = "No commits yet";
    if (date) date.textContent = "";
  }
  if (badge) badge.style.display = data.dirty ? "" : "none";
}

async function runBackup() {
  if (backupRunning) return;
  const btn    = document.getElementById("btn-backup");
  const output = document.getElementById("backup-output");
  if (!btn || btn.disabled) return;
  backupRunning = true;
  btn.disabled = true;
  btn.textContent = "Backing up…";
  output.style.display = "";
  output.className = "backup-output";
  output.textContent = "Running backup.sh…";
  try {
    const res  = await fetch("/api/backup/run", { method: "POST" });
    const data = await res.json();
    const text = data.output || (data.ok ? "Done." : data.error || "Unknown error");
    output.textContent = text;
    output.classList.add(data.ok ? "ok" : "err");
    if (data.ok) setTimeout(fetchBackupStatus, 500);
  } catch (e) {
    output.textContent = "Request failed: " + e.message;
    output.classList.add("err");
  } finally {
    backupRunning = false;
    btn.disabled = false;
    btn.textContent = "↑ Backup Now";
    fetchBackupStatus();
  }
}

// ── Utilities ──────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
