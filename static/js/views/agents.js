/**
 * views/agents.js — Agent status cards + lifecycle controls.
 */

import { store, metaFor } from "../state/store.js";
import { escHtml, formatDisplayTime } from "../util/format.js";
import * as api from "../api/client.js";

export function buildAgentCards() {
  const container = document.getElementById("agent-cards");
  if (!container) return;
  container.innerHTML = "";
  store.AGENT_IDS.forEach(id => {
    const meta = metaFor(id);
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
          <button class="agent-ctrl start"   data-agent="${id}" data-action="start"   title="Start gateway">▶</button>
          <button class="agent-ctrl stop"    data-agent="${id}" data-action="stop"    title="Stop gateway">⏸</button>
          <button class="agent-ctrl restart" data-agent="${id}" data-action="restart" title="Restart gateway">↻</button>
        </span>
      </div>
      <div class="agent-meta">
        <span id="seen-${id}">Never active</span>
        <span class="agent-session" id="session-${id}"></span>
        <span class="agent-pids" id="pids-${id}" style="font-size:0.75em;opacity:0.7;margin-left:6px;font-family:monospace"></span>
      </div>
      <div class="agent-action" id="action-${id}"></div>
    `;
    container.appendChild(card);
  });
}

/** Wire lifecycle button clicks (event delegation on agent-cards container). */
export function setupAgentCardActions() {
  const container = document.getElementById("agent-cards");
  if (!container || container.dataset.agentActionsBound) return;
  container.dataset.agentActionsBound = "1";
  container.addEventListener("click", async (e) => {
    const btn = e.target.closest(".agent-ctrl[data-agent]");
    if (!btn) return;
    const { agent, action } = btn.dataset;
    await agentLifecycleAction(agent, action);
  });
}

export async function agentLifecycleAction(agentId, action) {
  const wrap = document.getElementById(`controls-${agentId}`);
  if (wrap) wrap.classList.add("busy");
  try {
    const data = await api.agentLifecycle(agentId, action);
    if (!data.ok) {
      if (data.kind === "no_gateway") {
        alert(`${agentId} is a pure Kanban worker — no always-on gateway to ${action}.`);
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
    setTimeout(() => fetchAndApplyStatus(), 700);
  }
}

export function updateAgentCard(agentId, data) {
  const meta = metaFor(agentId);
  if (!meta) return;

  const state = store.agentState[agentId] || {};
  Object.assign(state, data);
  store.agentState[agentId] = state;

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
  if (state.session)    session.textContent = `session ${state.session}`;
  if (state.lastAction) action.textContent  = state.lastAction;

  const pidsEl = document.getElementById(`pids-${agentId}`);
  if (pidsEl) {
    const pids = state.pids || [];
    const wc   = state.worker_count || 0;
    if (pids.length > 0) {
      pidsEl.textContent = `PID${pids.length > 1 ? "s" : ""}: ${pids.join(", ")}`;
      pidsEl.title = `${wc} worker${wc !== 1 ? "s" : ""} running`;
    } else if (wc > 0) {
      pidsEl.textContent = `${wc} worker${wc !== 1 ? "s" : ""}`;
      pidsEl.title = "";
    } else {
      pidsEl.textContent = "";
      pidsEl.title = "";
    }
  }

  const controls = document.getElementById(`controls-${agentId}`);
  if (controls) {
    const hasGateway = state.has_gateway !== false;
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
        b.title = "Pure Kanban worker — no gateway. Spawned on demand.";
      });
    } else {
      if (btns.start)   { btns.start.disabled = running;  btns.start.title = running ? "Already running" : "Start gateway"; }
      if (btns.stop)    { btns.stop.disabled  = !running; btns.stop.title  = running ? "Stop gateway"    : "Already stopped"; }
      if (btns.restart) { btns.restart.disabled = !running; btns.restart.title = running ? "Restart gateway" : "Gateway is stopped"; }
    }
  }
}

/** Fetch /api/status and apply results to cards + kanban pills. */
let _fetchAndApplyStatus = null;
export function setStatusFetcher(fn) { _fetchAndApplyStatus = fn; }
function fetchAndApplyStatus() { _fetchAndApplyStatus?.(); }
