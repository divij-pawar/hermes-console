/**
 * views/activity.js — PostgreSQL tool activity panel.
 */

import { store, metaFor } from "../state/store.js";
import { escHtml, formatDisplayTime } from "../util/format.js";
import { scheduleRender, skipIfUnchanged } from "../util/renderQueue.js";

function _toolLabel(tool) {
  if (!tool) return "tool";
  if (tool === "read_file") return "read";
  if (tool.startsWith("memory:")) return tool.replace("memory:", "vector.");
  if (tool === "x:search") return "x.search";
  if (tool === "tavily:search") return "tavily.search";
  if (tool === "tavily:extract") return "tavily.extract";
  return tool;
}

function _toolClass(tool) {
  if (tool === "read_file")        return "read";
  if (tool === "memory:search")    return "memory-search";
  if (tool === "memory:store")     return "memory-store";
  if (tool === "memory:ingest")    return "memory-ingest";
  if (tool === "x:search")         return "x-search";
  if (tool === "tavily:search")    return "tavily-search";
  if (tool === "tavily:extract")   return "tavily-extract";
  return "tool";
}

function _detailText(row) {
  const extra = row.extra || {};
  const raw   = extra.query || extra.url || extra.display_path || extra.path || row.detail || "";
  if (!raw) return "";
  const text = String(raw);
  if (text.startsWith("query=")) return text.slice(6);
  if (text.startsWith("url="))   return text.slice(4);
  return text;
}

function _formatJsonish(value) {
  if (value == null || value === "") return "";
  if (typeof value === "string") {
    try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
  }
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

export function renderActivity(data, openLogViewerFn) {
  if (skipIfUnchanged("activity", data)) return;
  scheduleRender("activity", () => _paintActivity(data, openLogViewerFn));
}

function _paintActivity(data, openLogViewerFn) {
  const listEl   = document.getElementById("activity-list");
  const statusEl = document.getElementById("activity-status");
  const summaryEl = document.getElementById("activity-summary");
  if (!listEl) return;

  if (data.error) {
    if (statusEl)  statusEl.textContent = "error";
    listEl.innerHTML = `<div class="activity-empty">${escHtml(data.error)}</div>`;
    if (summaryEl) summaryEl.innerHTML = "";
    return;
  }

  const rows = data.rows || [];
  store.latestToolActivityRows = rows;
  if (statusEl) statusEl.textContent = `${rows.length}`;

  const counts = data.counts || {};
  if (summaryEl) {
    const bits = Object.keys(counts).sort().map(k =>
      `<span class="activity-chip chip-${escHtml(_toolClass(k))}">${escHtml(_toolLabel(k))}: ${counts[k]}</span>`
    );
    summaryEl.innerHTML = bits.join("");
  }

  if (!rows.length) {
    listEl.innerHTML = `<div class="activity-empty">No memory or file activity yet</div>`;
    return;
  }

  listEl.innerHTML = rows.map((row, idx) => {
    const agent      = row.agent || "unknown";
    const meta       = metaFor(agent);
    const toolClass  = _toolClass(row.tool);
    const detail     = _detailText(row);
    const extra      = row.extra || {};
    const result     = extra.result || extra.result_preview || "";
    const duration   = Number.isFinite(row.duration_ms) && row.duration_ms > 0
      ? `<span title="duration">${row.duration_ms}ms</span>` : "";
    return `
      <div class="activity-row tool-${escHtml(toolClass)} clickable" data-activity-index="${idx}" style="--agent-color:${meta.color}" title="${escHtml(detail)}">
        <div class="activity-row-head">
          <span class="activity-agent">${meta.emoji} ${escHtml(meta.label)}</span>
          <span class="activity-time">${escHtml(formatDisplayTime(row.ts || ""))}</span>
        </div>
        <div class="activity-tool">${escHtml(_toolLabel(row.tool))}</div>
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
      const row = store.latestToolActivityRows[Number(el.dataset.activityIndex)];
      if (row && openLogViewerFn) _openActivityViewer(row, el, openLogViewerFn);
    });
  });
}

function _openActivityViewer(row, rowEl, openLogViewerFn) {
  const extra    = row.extra || {};
  const query    = extra.cmd || extra.path || row.detail || "";
  const result   = extra.result || extra.result_preview || "";
  const meta     = Object.fromEntries(Object.entries(extra).filter(([k]) => !["result", "result_preview"].includes(k)));
  const isX      = row.tool === "x:search";
  const isTavily = row.tool?.startsWith("tavily:");
  const header   = isX ? "X Search Context" : isTavily ? "Tavily Context" : (row.tool === "read_file" ? "File / Query" : "Query / Command");
  const full = [
    `## ${header}\n${query || "(none)"}`,
    (isX || isTavily) && meta.args ? `## Parameters\n${_formatJsonish(meta.args)}` : "",
    `## Result Preview\n${_formatJsonish(result) || "(no result captured)"}`,
    Object.keys(meta).length ? `## Metadata\n${_formatJsonish(meta)}` : "",
  ].filter(Boolean).join("\n\n");
  openLogViewerFn({
    agent: row.agent || "unknown",
    kind:  row.tool === "read_file" ? "read_file" : "tool_call",
    title: _toolLabel(row.tool),
    detail: row.detail || "",
    full,
    ts:    row.ts || "",
  }, rowEl);
}
