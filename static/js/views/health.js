/**
 * views/health.js — Health issues panel.
 * Owns: issues Map, dismissed set, renderIssues().
 */

import { escHtml, formatDisplayTime } from "../util/format.js";
import { metaFor } from "../state/store.js";

const SEV_ICON  = { critical: "🔴", warning: "🟡", info: "🔵" };
const SEV_ORDER = { critical: 0, warning: 1, info: 2 };

const DISMISSED_KEY = "hermes-monitor:dismissed-issues";

const issues = new Map();

function _loadDismissed() {
  try {
    return new Set(JSON.parse(sessionStorage.getItem(DISMISSED_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

let dismissedIssues = _loadDismissed();

function _saveDismissed() {
  sessionStorage.setItem(DISMISSED_KEY, JSON.stringify([...dismissedIssues]));
}

export function dismissIssue(id) {
  dismissedIssues.add(id);
  _saveDismissed();
  renderIssues();
}

export function handleHealthInit(data) {
  issues.clear();
  (data.issues || []).forEach(iss => issues.set(iss.id, iss));
  renderIssues();
}

export function handleHealthAlert(data) {
  if (data.action === "raise") {
    issues.set(data.issue.id, data.issue);
  } else if (data.action === "resolve") {
    issues.delete(data.issue_id);
    dismissedIssues.delete(data.issue_id);
    _saveDismissed();
  }
  renderIssues();
}

export function renderIssues() {
  const panel    = document.getElementById("health-panel");
  const bar      = document.getElementById("health-bar");
  const issuesEl = document.getElementById("health-issues");
  const countEl  = document.getElementById("health-issue-count");
  if (!panel || !bar || !issuesEl) return;

  const list = [...issues.values()]
    .filter(i => !dismissedIssues.has(i.id))
    .sort((a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9));

  const hasCritical = list.some(i => i.severity === "critical");

  panel.className = list.length === 0 ? "health-clean"
                  : hasCritical       ? "health-critical"
                  : "health-warning";

  const okIcon = bar.querySelector(".health-ok-icon");
  const okText = bar.querySelector(".health-ok-text");

  if (list.length === 0) {
    if (okIcon) okIcon.style.display = "";
    if (okText) okText.style.display = "";
    if (countEl) countEl.style.display = "none";
    issuesEl.style.display = "none";
    issuesEl.innerHTML = "";
    return;
  }

  if (okIcon) okIcon.style.display = "none";
  if (okText) okText.style.display = "none";
  if (countEl) {
    countEl.textContent = `${list.length} issue${list.length > 1 ? "s" : ""}`;
    countEl.style.display = "";
  }
  issuesEl.style.display = "";

  issuesEl.innerHTML = list.map(iss => {
    const icon     = SEV_ICON[iss.severity] || "⚪";
    const agentMeta = iss.agent ? metaFor(iss.agent) : null;
    const agentBadge = agentMeta
      ? `<span class="issue-agent-badge" style="color:${agentMeta.color}">${agentMeta.emoji} ${escHtml(agentMeta.label)}</span>`
      : (iss.agent ? `<span class="issue-agent-badge">${escHtml(iss.agent)}</span>` : "");
    const ts     = iss.ts ? `<span class="issue-ts">${escHtml(formatDisplayTime(iss.ts))}</span>` : "";
    const detail = iss.detail ? `<div class="issue-detail">${escHtml(iss.detail)}</div>` : "";
    return `
      <div class="health-issue sev-${escHtml(iss.severity)}" data-id="${escHtml(iss.id)}">
        <span class="issue-sev-badge">${icon}</span>
        <div class="issue-body">
          <div class="issue-title">${escHtml(iss.title)}</div>
          ${detail}
        </div>
        ${agentBadge}
        ${ts}
        <button class="issue-dismiss" type="button" aria-label="Dismiss">✕</button>
      </div>`;
  }).join("");
}

/** Bind dismiss clicks — call once after DOMContentLoaded. */
export function setupHealthPanel() {
  document.getElementById("health-issues")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".issue-dismiss");
    if (!btn) return;
    const row = btn.closest(".health-issue");
    if (row?.dataset.id) dismissIssue(row.dataset.id);
  });
}
