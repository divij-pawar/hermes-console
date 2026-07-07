/**
 * views/cron.js — Cron jobs panel.
 */

import * as api from "../api/client.js";
import { escHtml } from "../util/format.js";

function _fmtTime(v) {
  if (!v) return "—";
  try {
    return new Date(v).toLocaleString(undefined, {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return v;
  }
}

export async function loadCronJobs() {
  const tbody = document.getElementById("cron-tbody");
  if (!tbody) return;
  try {
    const { jobs } = await api.fetchCron();
    if (!jobs?.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="cron-empty">No cron jobs configured</td></tr>';
      return;
    }
    tbody.innerHTML = jobs.map(j => {
      const isPaused    = j.paused === 1 || j.paused === true || j.status === "paused";
      const dot         = isPaused
        ? `<span style="color:var(--dim)">⏸</span>`
        : `<span style="color:var(--accent-g)">●</span>`;
      const toggleBtn   = isPaused
        ? `<button class="cron-btn cron-btn-resume" data-cron-id="${escHtml(j.id)}" data-cron-action="resume">▶ Resume</button>`
        : `<button class="cron-btn cron-btn-pause"  data-cron-id="${escHtml(j.id)}" data-cron-action="pause">❙❙ Pause</button>`;
      return `<tr>
        <td>${escHtml(j.name || j.id || "—")}</td>
        <td><span class="cron-sched">${escHtml(j.schedule || j.cron_expression || j.cron || "—")}</span></td>
        <td>${escHtml(j.profile || j.assignee || j.agent || "—")}</td>
        <td>${dot} ${isPaused ? "paused" : "active"}</td>
        <td>${_fmtTime(j.last_run || j.last_run_at)}</td>
        <td>${_fmtTime(j.next_run || j.next_run_at)}</td>
        <td style="white-space:nowrap">
          <button class="cron-btn cron-btn-run" data-cron-id="${escHtml(j.id)}" data-cron-action="run">▷ Run</button>
          ${toggleBtn}
        </td>
      </tr>`;
    }).join("");
  } catch {
    tbody.innerHTML = '<tr><td colspan="7" class="cron-empty" style="color:var(--accent-r)">Failed to load cron jobs</td></tr>';
  }
}

export async function cronAction(id, action) {
  try {
    const r = await api.cronAction(id, action);
    if (r.ok) loadCronJobs();
    else alert(`Cron ${action} failed: ${r.msg || r.error || "unknown error"}`);
  } catch (e) {
    alert("Request failed: " + e);
  }
}

/** Wire button clicks via event delegation on cron-tbody. */
export function setupCronActions() {
  document.getElementById("cron-tbody")?.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-cron-id]");
    if (!btn) return;
    cronAction(btn.dataset.cronId, btn.dataset.cronAction);
  });
}
