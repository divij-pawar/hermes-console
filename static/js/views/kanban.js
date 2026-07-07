/**
 * views/kanban.js — Kanban board, pills, card drawer.
 */

import { store } from "../state/store.js";
import { metaFor } from "../state/store.js";
import { escHtml, formatDisplayTime } from "../util/format.js";
import { scheduleRender, skipIfUnchanged } from "../util/renderQueue.js";
import { getPrefs } from "../util/prefs.js";
import * as api from "../api/client.js";

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

// ── Helpers ────────────────────────────────────────────────────────────────────

function _humanDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function _formatUnix(epoch) {
  if (!epoch) return "—";
  return formatDisplayTime(epoch, { date: true, tz: true });
}

// ── Board ──────────────────────────────────────────────────────────────────────

export function mergeKanbanCard(card) {
  const tasks = store.kanbanBoard.tasks || [];
  const i = tasks.findIndex(t => t.id === card.id);
  if (i >= 0) tasks[i] = card;
  else tasks.unshift(card);
  store.kanbanBoard.tasks = tasks;
}

export function renderKanbanBoard() {
  const fp = {
    tasks:  store.kanbanBoard.tasks || [],
    counts: store.kanbanBoard.counts || {},
  };
  if (skipIfUnchanged("kanban-board", fp)) return;
  scheduleRender("kanban-board", () => _paintKanbanBoard());
}

function _paintKanbanBoard() {
  const activeEl = document.getElementById("kanban-active");
  const doneEl   = document.getElementById("kanban-done");
  if (!activeEl || !doneEl) return;
  const tasks  = store.kanbanBoard.tasks || [];
  const active = tasks.filter(t => t.status !== "done" && t.status !== "archived");
  const done   = tasks.filter(t => t.status === "done");
  _renderKanbanCol(activeEl, active, "No active cards");
  _renderKanbanCol(doneEl, done, "No completed cards");
  renderKanbanPills();
}

function _renderKanbanCol(el, list, emptyText) {
  if (!list.length) {
    el.innerHTML = `<div class="kanban-empty">${emptyText}</div>`;
    return;
  }
  el.innerHTML = list.map(t => {
    const meta        = metaFor(t.assignee);
    const statusColor = KANBAN_STATUS_COLOR[t.status] || "#9aa0a6";
    const elapsed     = t.elapsed_s != null ? _humanDuration(t.elapsed_s) : "";
    return `
      <div class="kanban-card" data-id="${escHtml(t.id)}">
        <div class="kanban-card-row1">
          <span class="kanban-card-status" style="background:${statusColor}"></span>
          <span class="kanban-card-assignee" style="color:${meta.color}">${meta.emoji} ${meta.label}</span>
          <span class="kanban-card-elapsed">${elapsed}</span>
          ${t.status !== "done" ? `<button type="button" class="kanban-card-cancel" title="Cancel &amp; remove task">⊘</button>` : ""}
          <button type="button" class="kanban-card-archive" title="Archive card">✕</button>
        </div>
        <div class="kanban-card-title">${escHtml(t.title || "")}</div>
        <div class="kanban-card-id">${t.id} · ${t.status}</div>
      </div>
    `;
  }).join("");
}

export function renderKanbanPills() {
  const counts  = store.kanbanBoard.counts || {};
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

// ── Card actions ───────────────────────────────────────────────────────────────

export function setupKanbanCardActions() {
  ["kanban-active", "kanban-done"].forEach(colId => {
    const col = document.getElementById(colId);
    if (!col || col.dataset.kanbanActionsBound) return;
    col.dataset.kanbanActionsBound = "1";
    col.addEventListener("click", (e) => {
      const card = e.target.closest(".kanban-card");
      if (!card?.dataset.id) return;
      const taskId = card.dataset.id;
      if (e.target.closest(".kanban-card-cancel")) {
        e.preventDefault(); e.stopPropagation();
        cancelCard(taskId);
        return;
      }
      if (e.target.closest(".kanban-card-archive")) {
        e.preventDefault(); e.stopPropagation();
        archiveCard(taskId);
        return;
      }
      openCardDrawer(taskId);
    });
  });
}

export async function cancelCard(taskId) {
  if (!taskId) return;
  try {
    const data = await api.cancelCard(taskId);
    if (!data.ok) {
      alert(`Cancel failed: ${data.error || "unknown error"}`);
      return;
    }
    store.kanbanBoard.tasks = (store.kanbanBoard.tasks || []).filter(t => t.id !== taskId);
    if (store.openCardId === taskId) closeCardDrawer();
    renderKanbanBoard();
  } catch (e) {
    console.warn("cancel request failed", e);
  }
}

export async function archiveCard(taskId) {
  if (!taskId) return;
  try {
    const data = await api.archiveCard(taskId);
    if (!data.ok) {
      alert(`Archive failed: ${data.error || "unknown error"}`);
      return;
    }
    store.kanbanBoard.tasks = (store.kanbanBoard.tasks || []).filter(t => t.id !== taskId);
    if (store.openCardId === taskId) closeCardDrawer();
    renderKanbanBoard();
  } catch (e) {
    console.warn("archive request failed", e);
  }
}

export async function archiveAllDone() {
  const doneCount = (store.kanbanBoard.tasks || []).filter(t => t.status === "done").length;
  if (!doneCount) return;
  if (!confirm(`Archive ${doneCount} completed card${doneCount === 1 ? "" : "s"}?`)) return;
  try {
    const data = await api.archiveDoneCards();
    if (!data.ok) { alert("Archive failed: " + (data.error || "unknown")); return; }
    store.kanbanBoard.tasks = (store.kanbanBoard.tasks || []).filter(t => t.status !== "done");
    renderKanbanBoard();
  } catch (e) {
    alert("Archive request failed: " + e.message);
  }
}

// ── Card drawer ────────────────────────────────────────────────────────────────

export async function openCardDrawer(taskId) {
  const drawer = document.getElementById("card-drawer");
  if (!drawer) return;
  store.openCardId = taskId;
  store.openCardStatus = null;
  drawer.classList.add("open");
  document.getElementById("card-drawer-id").textContent = taskId;
  document.getElementById("card-drawer-status").textContent = "loading…";
  document.getElementById("card-drawer-body").innerHTML = `<div class="card-drawer-loading">Loading…</div>`;
  await refreshCardDrawer(taskId);
}

export function closeCardDrawer() {
  store.openCardId = null;
  store.openCardStatus = null;
  document.getElementById("card-drawer")?.classList.remove("open");
}

/** Reload drawer content; quiet=true skips loading placeholder on refresh. */
export async function refreshCardDrawer(taskId, { quiet = false } = {}) {
  if (!taskId || store.openCardId !== taskId) return;
  if (!quiet) {
    document.getElementById("card-drawer-status").textContent = "loading…";
    document.getElementById("card-drawer-body").innerHTML = `<div class="card-drawer-loading">Loading…</div>`;
  }
  try {
    const data = await api.fetchKanbanCard(taskId);
    if (data.error) {
      document.getElementById("card-drawer-body").innerHTML = `<div class="card-drawer-error">${escHtml(data.error)}</div>`;
      return;
    }
    store.openCardStatus = data.task?.status || null;
    _renderCardDrawer(data);
  } catch (e) {
    document.getElementById("card-drawer-body").innerHTML = `<div class="card-drawer-error">Request failed: ${escHtml(e.message)}</div>`;
  }
}

export function shouldPollOpenDrawer() {
  if (!store.openCardId || !getPrefs().drawerAutoRefresh) return false;
  const st = store.openCardStatus;
  return !st || ["running", "ready", "todo", "triage", "blocked", "scheduled"].includes(st);
}

function _renderCardDrawer(data) {
  const t    = data.task || {};
  const meta = metaFor(t.assignee);

  document.getElementById("card-drawer-id").innerHTML =
    `<span style="color:${meta.color}">${meta.emoji} ${meta.label}</span> · ${t.id}`;
  document.getElementById("card-drawer-status").innerHTML =
    `<span class="card-status-pill" style="background:${KANBAN_STATUS_COLOR[t.status] || "#9aa0a6"}">${t.status}</span>`;

  const created   = t.created_at   ? _formatUnix(t.created_at)   : "?";
  const started   = t.started_at   ? _formatUnix(t.started_at)   : "—";
  const completed = t.completed_at ? _formatUnix(t.completed_at) : "—";
  const elapsed   = (t.started_at && t.completed_at)
    ? _humanDuration(t.completed_at - t.started_at)
    : (t.started_at ? _humanDuration(Math.floor(Date.now() / 1000) - t.started_at) + " · running" : "—");

  const events = (data.events || []).map(ev => {
    const ts      = _formatUnix(ev.created_at);
    const payload = ev.payload && Object.keys(ev.payload).length
      ? `<pre class="card-event-payload">${escHtml(JSON.stringify(ev.payload, null, 2))}</pre>` : "";
    return `
      <div class="card-event">
        <div class="card-event-head"><span class="card-event-kind">${ev.kind}</span><span class="card-event-ts">${ts}</span></div>
        ${payload}
      </div>`;
  }).join("");

  const runs = (data.runs || []).map(r => {
    const meta2 = r.metadata || {};
    const metaPretty = Object.keys(meta2).length
      ? `<pre class="card-event-payload">${escHtml(JSON.stringify(meta2, null, 2))}</pre>` : "";
    const imgPath    = meta2.image_path;
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
        ${imgPreview}${metaPretty}
        ${r.error ? `<div class="card-run-error">${escHtml(r.error)}</div>` : ""}
      </div>`;
  }).join("");

  const comments = (data.comments || []).map(c =>
    `<div class="card-comment">
       <div class="card-comment-head">${escHtml(c.author || "?")} · ${_formatUnix(c.created_at)}</div>
       <div class="card-comment-body">${escHtml(c.body || "")}</div>
     </div>`
  ).join("") || `<div class="card-empty">No comments</div>`;

  const logTail  = (data.log_tail || []).map(escHtml).join("\n");
  const linksHtml = `
    ${data.parents?.length ? `<div>Parents: ${data.parents.join(", ")}</div>` : ""}
    ${data.children?.length ? `<div>Children: ${data.children.join(", ")}</div>` : ""}
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
        <div><b>workspace</b> <code>${escHtml(t.workspace_kind || "?")}${t.workspace_path ? ` · ${escHtml(t.workspace_path)}` : ""}</code></div>
      </div>
    </div>
    ${t.body ? `<div class="card-section"><div class="card-section-title">Brief</div><pre class="card-body">${escHtml(t.body)}</pre></div>` : ""}
    <div class="card-section">
      <div class="card-section-title">Runs (${data.runs?.length || 0})</div>
      ${runs || `<div class="card-empty">No runs yet</div>`}
    </div>
    <div class="card-section">
      <div class="card-section-title">Events (${data.events?.length || 0})</div>
      ${events || `<div class="card-empty">No events</div>`}
    </div>
    <div class="card-section">
      <div class="card-section-title">Comments</div>
      ${comments}
    </div>
    ${logTail ? `<div class="card-section"><div class="card-section-title">Worker log (tail)</div><pre class="card-log">${logTail}</pre></div>` : ""}
  `;
}
