/**
 * views/logs.js — Live log viewer (gateway / imagine tabs).
 */

import { escHtml } from "../util/format.js";

const MAX_LOG_LINES = 400;

let currentLogSource = "gateway";
const logBuffers = {};

function _detectLevel(line) {
  if (line.includes("[DEBUG"))   return "DEBUG";
  if (line.includes("[WARN") || line.includes("[WARNING")) return "WARNING";
  if (line.includes("[ERROR"))   return "ERROR";
  return "INFO";
}

function _formatLogLine(text) {
  let s = escHtml(text);
  s = s.replace(/(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})/g, '<span class="log-ts">$1</span>');
  s = s.replace(/(\[\d{2}:\d{2}:\d{2}\])/g, '<span class="log-ts">$1</span>');
  s = s.replace(/\[(INFO\s*)\]/g,    '<span class="log-lvl-info">[$1]</span>');
  s = s.replace(/\[(DEBUG\s*)\]/g,   '<span class="log-lvl-debug">[$1]</span>');
  s = s.replace(/\[(WARNING\s*)\]/g, '<span class="log-lvl-warn">[$1]</span>');
  s = s.replace(/\[(ERROR\s*)\]/g,   '<span class="log-lvl-error">[$1]</span>');
  s = s.replace(/\b(sage|imagine):/g, '<span class="log-agent">$1:</span>');
  s = s.replace(/(✅)/g, '<span class="log-ok">$1</span>');
  s = s.replace(/(⚠|warning)/gi, '<span class="log-lvl-warn">$1</span>');
  return s;
}

export async function loadInitialLogs(source) {
  try {
    const res  = await fetch(`/api/logs?source=${source}&lines=200`);
    const data = await res.json();
    if (data.lines?.length > 0) {
      const buf = logBuffers[source] || (logBuffers[source] = []);
      data.lines.forEach(line => {
        buf.push({ level: _detectLevel(line), text: line });
      });
      if (source === currentLogSource) renderLogs();
    }
  } catch {
    // ignore
  }
}

export function appendLogLine(source, level, text) {
  const buf = logBuffers[source] || (logBuffers[source] = []);
  buf.push({ level, text });
  if (buf.length > MAX_LOG_LINES) buf.shift();
  if (source === currentLogSource) _renderLogLine(level, text);
}

export function switchLog(source) {
  currentLogSource = source;
  document.querySelectorAll(".log-tab").forEach(t => t.classList.remove("active"));
  const tabEl = document.getElementById(`tab-${source}`);
  if (tabEl) tabEl.classList.add("active");
  renderLogs();
}

export function renderLogs() {
  const container = document.getElementById("log-content");
  if (!container) return;
  const buf = logBuffers[currentLogSource] || [];
  if (!buf.length) {
    container.innerHTML = '<div class="log-empty">No log entries yet</div>';
    return;
  }
  container.innerHTML = "";
  buf.forEach(({ level, text }) => {
    const el = document.createElement("div");
    el.className = `log-line ${level}`;
    el.innerHTML = _formatLogLine(text);
    container.appendChild(el);
  });
  container.scrollTop = container.scrollHeight;
}

function _renderLogLine(level, text) {
  const container = document.getElementById("log-content");
  if (!container) return;
  const empty = container.querySelector(".log-empty");
  if (empty) empty.remove();

  const wasAtBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 60;
  const el = document.createElement("div");
  el.className = `log-line ${level}`;
  el.innerHTML = _formatLogLine(text);
  container.appendChild(el);

  const lines = container.querySelectorAll(".log-line");
  if (lines.length > MAX_LOG_LINES) lines[0].remove();
  if (wasAtBottom) container.scrollTop = container.scrollHeight;
}
