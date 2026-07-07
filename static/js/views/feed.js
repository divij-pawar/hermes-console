/**
 * views/feed.js — Activity feed (SSE events, newest-first list).
 */

import { metaFor } from "../state/store.js";
import { escHtml, formatDisplayTime } from "../util/format.js";

const KIND_ICON = {
  tool_call:       "🔧",
  user_message:    "📨",
  response:        "💬",
  delegation:      "🎯",
  file_write:      "📄",
  tool_result:     "✅",
  tool_error:      "❌",
  subagent_result: "📬",
};

const MAX_FEED_EVENTS = 500;

let feedCount = 0;
let atTop     = true;
let feedFilter = "all";

/** Maps call_id → feed row element for attaching tool results. */
const feedRowsByCallId = new Map();

export function setupFeedScroll() {
  const list = document.getElementById("feed-list");
  if (!list) return;
  list.addEventListener("scroll", () => {
    atTop = list.scrollTop < 40;
  }, { passive: true });
}

export function setFeedFilter(value) {
  feedFilter = value || "all";
  document.querySelectorAll("#feed-list .event-row").forEach(row => {
    row.style.display = feedFilter === "all" || row.dataset.feedCategory === feedFilter ? "" : "none";
  });
}

export function clearFeed() {
  const list = document.getElementById("feed-list");
  if (!list) return;
  list.querySelectorAll(".event-row").forEach(r => r.remove());
  feedRowsByCallId.clear();
  feedCount = 0;
  const countEl = document.getElementById("feed-count");
  if (countEl) countEl.textContent = "0 events";
  const empty = document.getElementById("feed-empty");
  if (empty) empty.style.display = "";
}

export function appendFeedEvent(ev, openFileViewerFn, openLogViewerFn) {
  const list  = document.getElementById("feed-list");
  const empty = document.getElementById("feed-empty");
  if (!list) return;
  if (empty) empty.style.display = "none";

  const meta      = metaFor(ev.agent);
  const icon      = KIND_ICON[ev.kind] || "•";
  const timeLabel = formatDisplayTime(ev.ts || Date.now());

  const row = document.createElement("div");
  row.className = `event-row kind-${ev.kind}`;
  row.dataset.feedCategory = feedCategory(ev);
  row.__feedEvent = ev;
  if (!_feedEventVisible(ev)) row.style.display = "none";

  const isFileEvent = ev.kind === "file_write" && ev.detail;
  if (isFileEvent && openFileViewerFn) {
    row.classList.add("clickable");
    row.onclick = () => openFileViewerFn(ev.detail, ev.agent);
    row.title = `Click to view: ${ev.detail}`;
  } else if (ev.full && openLogViewerFn) {
    row.classList.add("clickable");
    row.onclick = () => openLogViewerFn(row.__feedEvent, row);
  }

  const showDetail = _feedShowsDetail(ev);
  row.innerHTML = `
    <span class="event-ts">${escHtml(timeLabel)}</span>
    <span class="agent-badge badge-${ev.agent}">${escHtml(meta.label)}</span>
    <span class="event-icon">${icon}</span>
    <span class="event-body">
      <span class="event-title" title="${escHtml(ev.detail || ev.title || "")}">${escHtml(ev.title || "")}</span>
      ${showDetail ? `<span class="event-detail">${escHtml(ev.detail)}</span>` : ""}
    </span>
    <span class="event-result-pill" style="display:none">result</span>
  `;
  if (ev.result_full) {
    const pill = row.querySelector(".event-result-pill");
    if (pill) pill.style.display = "";
  }

  if (list.firstChild) list.insertBefore(row, list.firstChild);
  else list.appendChild(row);

  const rows = list.querySelectorAll(".event-row");
  if (rows.length > MAX_FEED_EVENTS) rows[rows.length - 1].remove();

  feedCount++;
  const countEl = document.getElementById("feed-count");
  if (countEl) countEl.textContent = `${feedCount} event${feedCount !== 1 ? "s" : ""}`;

  if (atTop) list.scrollTop = 0;
  if (ev.call_id) feedRowsByCallId.set(ev.call_id, row);
}

export function attachToolResult(resultEv, openLogViewerFn) {
  const row = feedRowsByCallId.get(resultEv.call_id);
  if (!row) return false;

  const ev = row.__feedEvent || {};
  ev.result_title  = resultEv.title  || "";
  ev.result_detail = resultEv.detail || "";
  ev.result_full   = resultEv.full   || "";
  ev.result_ts     = resultEv.ts     || "";
  ev.result_kind   = resultEv.kind   || "tool_result";
  row.__feedEvent  = ev;

  row.classList.add(resultEv.kind === "tool_error" ? "has-tool-error" : "has-tool-result");
  row.classList.add("clickable");
  if (openLogViewerFn) row.onclick = () => openLogViewerFn(row.__feedEvent, row);

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

// ── Internals ──────────────────────────────────────────────────────────────────

function feedCategory(ev) {
  const text = `${ev.kind || ""} ${ev.title || ""} ${ev.detail || ""}`.toLowerCase();
  if ((ev.kind || "").includes("error") || text.includes("error") || text.includes("failed")) return "errors";
  if (text.includes("model #") || text.includes("api call")) return "model";
  if (text.includes("memory") || text.includes("vector")) return "memory";
  if (ev.kind === "delegation" || text.includes("kanban")) return "kanban";
  if (ev.kind === "tool_call" || ev.kind === "tool_result") return "tools";
  return "all";
}

function _feedEventVisible(ev) {
  return feedFilter === "all" || feedCategory(ev) === feedFilter;
}

function _feedShowsDetail(ev) {
  const detail = (ev.detail || "").trim();
  const title  = (ev.title  || "").trim();
  if (!detail) return false;
  if (detail === title) return false;
  if (title.includes(detail)) return false;
  return true;
}
