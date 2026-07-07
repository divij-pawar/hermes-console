/**
 * views/traces.js — Slack prompt trace panel.
 */

import { store } from "../state/store.js";
import { escHtml, fmtNum, fmtMoney, fmtShortTime } from "../util/format.js";
import { scheduleRender, skipIfUnchanged } from "../util/renderQueue.js";

export function upsertPromptTrace(trace) {
  if (!trace?.id) return;
  const idx = store.promptTraces.findIndex(t => t.id === trace.id);
  if (idx >= 0) store.promptTraces[idx] = trace;
  else store.promptTraces.unshift(trace);
  store.promptTraces.sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
  store.promptTraces = store.promptTraces.slice(0, 30);
}

export function renderPromptTraces(openLogViewerFn) {
  if (skipIfUnchanged("traces", store.promptTraces)) return;
  scheduleRender("traces", () => _paintPromptTraces(openLogViewerFn));
}

function _paintPromptTraces(openLogViewerFn) {
  const list  = document.getElementById("prompt-trace-list");
  const count = document.getElementById("prompt-trace-count");
  if (!list) return;
  if (count) count.textContent = `${store.promptTraces.length}`;

  if (!store.promptTraces.length) {
    list.innerHTML = `<div class="prompt-trace-empty">No Slack prompt traces yet</div>`;
    return;
  }

  list.innerHTML = store.promptTraces.slice(0, 8).map((trace, idx) => {
    const usage   = trace.usage || {};
    const elapsed = trace.ended_at && trace.started_at
      ? `${Math.round(trace.ended_at - trace.started_at)}s`
      : "running";
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

  if (openLogViewerFn) {
    list.querySelectorAll(".prompt-trace-row[data-trace-index]").forEach(el => {
      el.addEventListener("click", () => {
        const trace = store.promptTraces[Number(el.dataset.traceIndex)];
        if (trace) _openPromptTraceViewer(trace, el, openLogViewerFn);
      });
    });
  }
}

function _openPromptTraceViewer(trace, rowEl, openLogViewerFn) {
  const usage   = trace.usage || {};
  const events  = (trace.events || []).map(e => {
    const when = e.ts ? fmtShortTime(e.ts) : "";
    return `- ${when} ${e.kind}: ${e.detail || ""}`;
  }).join("\n");
  const full = [
    `## Slack Request\n${trace.msg || "(none)"}`,
    `## Summary\nstatus: ${trace.status || "running"}\nelapsed: ${trace.ended_at && trace.started_at ? Math.round(trace.ended_at - trace.started_at) + "s" : "running"}\nmodel calls: ${usage.calls || 0}\ntokens: ${usage.input || 0} input / ${usage.output || 0} output\nestimated cost: ${fmtMoney(usage.cost || 0)}`,
    `## Timeline\n${events || "(no events yet)"}`,
    trace.final ? `## Final Response\n${trace.final}` : "",
  ].filter(Boolean).join("\n\n");
  openLogViewerFn({
    agent:  trace.agent || "sage",
    kind:   "prompt_trace",
    title:  "Prompt Trace",
    detail: trace.msg || "",
    full,
    ts:     trace.started_at ? fmtShortTime(trace.started_at) : "",
  }, rowEl);
}
