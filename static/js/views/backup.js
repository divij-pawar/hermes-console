/**
 * views/backup.js — Config backup panel.
 */

import * as api from "../api/client.js";
import { escHtml } from "../util/format.js";

let _backupRunning = false;

export async function fetchAndRenderBackupStatus() {
  try {
    const data = await api.fetchBackupStatus();
    _updateBackupPanel(data);
  } catch {
    const commit = document.getElementById("backup-commit");
    if (commit) commit.textContent = "Unavailable";
  }
}

function _updateBackupPanel(data) {
  const commit  = document.getElementById("backup-commit");
  const date    = document.getElementById("backup-date");
  const badge   = document.getElementById("backup-dirty-badge");
  const repoEl  = document.getElementById("backup-repo");
  const btn     = document.getElementById("btn-backup");

  if (repoEl) {
    if (data.repo) {
      repoEl.textContent = data.repo;
      repoEl.title       = data.repo;
      repoEl.style.display = "";
    } else {
      repoEl.textContent = "";
      repoEl.style.display = "none";
    }
  }

  if (btn) {
    btn.disabled = data.configured === false;
    btn.title    = data.configured === false
      ? "Clone sf_agents or set HERMES_BACKUP_REPO to enable"
      : "Run backup.sh: ~/.hermes → sf_agents git commit";
  }

  if (!data.ok) {
    if (commit) commit.textContent = data.error || "Unavailable";
    if (date)   date.textContent   = "";
    if (badge)  badge.style.display = "none";
    return;
  }
  if (data.last_commit) {
    const h = data.last_commit.hash ? `${data.last_commit.hash} · ` : "";
    if (commit) commit.textContent = h + (data.last_commit.message || "No commits yet");
    if (date)   date.textContent   = data.last_commit.date || "";
  } else {
    if (commit) commit.textContent = "No commits yet";
    if (date)   date.textContent   = "";
  }
  if (badge) badge.style.display = data.dirty ? "" : "none";
}

export async function runBackup() {
  if (_backupRunning) return;
  const btn    = document.getElementById("btn-backup");
  const output = document.getElementById("backup-output");
  if (!btn || btn.disabled) return;

  _backupRunning        = true;
  btn.disabled          = true;
  btn.textContent       = "Backing up…";
  if (output) {
    output.style.display = "";
    output.className    = "backup-output";
    output.textContent  = "Running backup.sh…";
  }
  try {
    const data = await api.runBackup();
    const text = data.output || (data.ok ? "Done." : data.error || "Unknown error");
    if (output) { output.textContent = text; output.classList.add(data.ok ? "ok" : "err"); }
    if (data.ok) setTimeout(fetchAndRenderBackupStatus, 500);
  } catch (e) {
    if (output) { output.textContent = "Request failed: " + e.message; output.classList.add("err"); }
  } finally {
    _backupRunning  = false;
    btn.disabled    = false;
    btn.textContent = "↑ Backup Now";
    fetchAndRenderBackupStatus();
  }
}
