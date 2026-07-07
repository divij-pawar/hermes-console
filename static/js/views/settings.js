/**
 * views/settings.js — Settings side-panel.
 */

import * as api from "../api/client.js";
import { getPrefs, savePrefs, darkModeEnabled } from "../util/prefs.js";
import { relayoutSidebarLayouts } from "./layout.js";

// ── Panel open / close ────────────────────────────────────────────────────────

export function openSettings()  {
  document.getElementById("settings-panel")?.classList.add("sp-open");
  document.getElementById("settings-overlay")?.classList.add("sp-open");
  loadSettingsValues();
}

export function closeSettings() {
  document.getElementById("settings-panel")?.classList.remove("sp-open");
  document.getElementById("settings-overlay")?.classList.remove("sp-open");
}

export function toggleSettings() {
  const open = document.getElementById("settings-panel")?.classList.contains("sp-open");
  open ? closeSettings() : openSettings();
}

export function switchSettingsTab(tab) {
  document.querySelectorAll("#settings-tabs-bar .stab").forEach(btn => {
    btn.classList.toggle("stab-active", btn.dataset.tab === tab);
  });
  ["connection", "monitoring", "backup", "data", "panels", "performance", "debug"].forEach(t => {
    const el = document.getElementById("stab-" + t);
    if (el) el.style.display = t === tab ? "block" : "none";
  });
}

// ── Load / save ───────────────────────────────────────────────────────────────

export async function loadSettingsValues() {
  try {
    const s = await api.fetchSettings();

    _setVal("cfg-hermes-dir",   s.hermes_dir     || "");
    _setVal("cfg-orchestrator", s.orchestrator   || "");
    _setVal("cfg-docker",       s.docker_container || "");
    _setVal("cfg-port",         s.webui_port     || 7979);
    _setVal("cfg-host",         s.webui_host     || "127.0.0.1");
    _setVal("cfg-agent-plists", s.agent_plists   || "");
    _setVal("cfg-agent-labels", s.agent_labels   || "");
    _setVal("cfg-trace-mode",   s.trace_mode     || "milestones");
    _setVal("cfg-slack-channel",s.slack_trace_channel || "");
    _setVal("cfg-warmup",       s.warmup_freshness || 21600);
    _setVal("cfg-media-dir",    s.media_dir      || "");
    _setVal("cfg-media-agent",  s.media_agent    || "");
    _setVal("cfg-backup-repo",  s.backup_repo    || "");
    _setVal("cfg-backup-script",s.backup_script  || "");
    _setVal("cfg-vector-db",    s.vector_db_url  || "");
    _setVal("cfg-files-age",    s.files_max_age_days || 14);
    _setVal("cfg-files-count",  s.files_max_entries  || 80);

    const traceChip = document.getElementById("sfield-trace-chip");
    if (traceChip) {
      if (s.trace_enabled) {
        traceChip.innerHTML = `<div class="s-chip s-chip-ok">✓ Trace active — posting to <code>${s.slack_trace_channel || "?"}</code></div>`;
      } else if (s.slack_trace_channel) {
        traceChip.innerHTML = `<div class="s-chip s-chip-warn">⚠ Channel set but SLACK_BOT_TOKEN missing in ${_shortDir(s.hermes_dir)}/.env</div>`;
      } else {
        traceChip.innerHTML = `<div class="s-chip s-chip-off">○ Trace disabled — set channel + token to enable</div>`;
      }
    }

    const backupChip = document.getElementById("sfield-backup-chip");
    if (backupChip) {
      if (s.backup_configured) {
        backupChip.innerHTML = `<div class="s-chip s-chip-ok">✓ Backup configured</div>`;
      } else if (s.backup_repo) {
        backupChip.innerHTML = `<div class="s-chip s-chip-warn">⚠ Repo set but script missing or not found</div>`;
      } else {
        backupChip.innerHTML = `<div class="s-chip s-chip-off">○ Not configured — set repo and script paths</div>`;
      }
    }

    const prefs = getPrefs();
    _setChecked("toggle-dark",    darkModeEnabled(prefs));
    _setChecked("toggle-compact", prefs.compact || false);
    ["usage", "backup", "traces", "activity", "files", "cron"].forEach(p => {
      _setChecked("panel-" + p, prefs["panel_" + p] !== false);
    });

    _setVal("pref-poll-preset", prefs.pollPreset || "normal");
    _setChecked("pref-pause-hidden", prefs.pauseWhenHidden !== false);
    _setChecked("pref-pause-dragging", prefs.pauseWhenDragging !== false);
    _setChecked("pref-sse-backoff", prefs.sseBackoff !== false);
    _setChecked("pref-drawer-refresh", prefs.drawerAutoRefresh !== false);
    _setVal("pref-drawer-poll-sec", prefs.drawerPollSec ?? 10);
    _setVal("pref-activity-poll-sec", prefs.activityPollSec ?? 10);
    _setVal("pref-manual-multiplier", prefs.manualPollMultiplier ?? 1);
    _setChecked("pref-client-debug", prefs.clientDebug || false);
    _setChecked("pref-server-debug-log", prefs.serverDebugLog || false);
    _toggleManualPollFields(prefs.pollPreset === "manual");
  } catch (e) {
    console.warn("loadSettingsValues failed:", e);
  }
}

export async function saveSettings(tab) {
  const statusEl = document.getElementById("status-" + tab);
  if (statusEl) { statusEl.textContent = "Saving…"; statusEl.className = "sfield-status"; }

  const FIELDS = {
    connection: () => ({
      hermes_dir:       document.getElementById("cfg-hermes-dir")?.value    || "",
      orchestrator:     document.getElementById("cfg-orchestrator")?.value  || "",
      docker_container: document.getElementById("cfg-docker")?.value        || "",
      webui_port:       parseInt(document.getElementById("cfg-port")?.value || 7979),
      webui_host:       document.getElementById("cfg-host")?.value          || "",
      agent_plists:     document.getElementById("cfg-agent-plists")?.value  || "",
      agent_labels:     document.getElementById("cfg-agent-labels")?.value  || "",
    }),
    monitoring: () => ({
      trace_mode:          document.getElementById("cfg-trace-mode")?.value    || "milestones",
      slack_trace_channel: document.getElementById("cfg-slack-channel")?.value || "",
      warmup_freshness:    parseInt(document.getElementById("cfg-warmup")?.value || 21600),
      media_dir:           document.getElementById("cfg-media-dir")?.value      || "",
      media_agent:         document.getElementById("cfg-media-agent")?.value    || "",
    }),
    backup: () => ({
      backup_repo:   document.getElementById("cfg-backup-repo")?.value   || "",
      backup_script: document.getElementById("cfg-backup-script")?.value || "",
    }),
    data: () => ({
      vector_db_url:      document.getElementById("cfg-vector-db")?.value     || "",
      files_max_age_days: parseInt(document.getElementById("cfg-files-age")?.value   || 14),
      files_max_entries:  parseInt(document.getElementById("cfg-files-count")?.value || 80),
    }),
  };

  const getter = FIELDS[tab];
  if (!getter) return;

  try {
    const r = await api.saveSettings(getter());
    if (r.ok) {
      if (statusEl) { statusEl.textContent = "✓ Saved — restart the server to apply changes"; statusEl.className = "sfield-status ok"; }
    } else {
      if (statusEl) { statusEl.textContent = "✗ Error: " + (r.error || "unknown"); statusEl.className = "sfield-status err"; }
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = "✗ Request failed: " + e; statusEl.className = "sfield-status err"; }
  }
}

// ── Appearance ────────────────────────────────────────────────────────────────

export function applyAppearance() {
  const prefs    = getPrefs();
  prefs.dark     = document.getElementById("toggle-dark")?.checked    ?? true;
  prefs.compact  = document.getElementById("toggle-compact")?.checked || false;
  savePrefs(prefs);
  document.body.classList.toggle("dark-mode",    darkModeEnabled(prefs));
  document.body.classList.toggle("light-mode",   !darkModeEnabled(prefs));
  document.body.classList.toggle("compact-mode", prefs.compact);
}

export function applyPanelVisibility() {
  const prefs = getPrefs();
  const PANEL_MAP = {
    usage:    "#usage-panel",
    backup:   "#backup-panel",
    traces:   "#prompt-trace-panel",
    activity: "#activity-panel",
    files:    "#files-panel",
    cron:     "#cron-panel",
  };
  ["usage", "backup", "traces", "activity", "files", "cron"].forEach(p => {
    const cb = document.getElementById("panel-" + p);
    const visible = cb ? cb.checked : true;
    prefs["panel_" + p] = visible;
    const sel = PANEL_MAP[p];
    if (sel) document.querySelectorAll(sel).forEach(el => { el.style.display = visible ? "" : "none"; });
  });
  savePrefs(prefs);
  relayoutSidebarLayouts();
}

export function applyPerformancePrefs() {
  const prefs = getPrefs();
  prefs.pollPreset = document.getElementById("pref-poll-preset")?.value || "normal";
  prefs.pauseWhenHidden = document.getElementById("pref-pause-hidden")?.checked !== false;
  prefs.pauseWhenDragging = document.getElementById("pref-pause-dragging")?.checked !== false;
  prefs.sseBackoff = document.getElementById("pref-sse-backoff")?.checked !== false;
  prefs.drawerAutoRefresh = document.getElementById("pref-drawer-refresh")?.checked !== false;
  prefs.drawerPollSec = parseInt(document.getElementById("pref-drawer-poll-sec")?.value || 10);
  prefs.activityPollSec = parseInt(document.getElementById("pref-activity-poll-sec")?.value || 10);
  prefs.manualPollMultiplier = parseFloat(document.getElementById("pref-manual-multiplier")?.value || 1);
  savePrefs(prefs);
  _toggleManualPollFields(prefs.pollPreset === "manual");
  const statusEl = document.getElementById("status-performance");
  if (statusEl) {
    statusEl.textContent = "✓ Performance prefs saved";
    statusEl.className = "sfield-status ok";
  }
}

export function applyDebugPrefs() {
  const prefs = getPrefs();
  prefs.clientDebug = document.getElementById("pref-client-debug")?.checked || false;
  prefs.serverDebugLog = document.getElementById("pref-server-debug-log")?.checked || false;
  savePrefs(prefs);
  const statusEl = document.getElementById("status-debug");
  if (statusEl) {
    statusEl.textContent = "✓ Debug prefs saved";
    statusEl.className = "sfield-status ok";
  }
}

export function onPollPresetChange() {
  const preset = document.getElementById("pref-poll-preset")?.value || "normal";
  _toggleManualPollFields(preset === "manual");
}

export function restorePrefsOnLoad() {
  const prefs = getPrefs();
  document.body.classList.toggle("dark-mode",  darkModeEnabled(prefs));
  document.body.classList.toggle("light-mode", !darkModeEnabled(prefs));
  if (prefs.compact) document.body.classList.add("compact-mode");
  const PANEL_MAP = {
    usage:    "#usage-panel",
    backup:   "#backup-panel",
    traces:   "#prompt-trace-panel",
    activity: "#activity-panel",
    files:    "#files-panel",
    cron:     "#cron-panel",
  };
  ["usage", "backup", "traces", "activity", "files", "cron"].forEach(p => {
    if (prefs["panel_" + p] === false) {
      const sel = PANEL_MAP[p];
      if (sel) document.querySelectorAll(sel).forEach(el => { el.style.display = "none"; });
    }
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _setVal(id, val)     { const el = document.getElementById(id); if (el) el.value   = val; }
function _setChecked(id, val) { const el = document.getElementById(id); if (el) el.checked = val; }
function _shortDir(dir) { if (!dir) return "~/.hermes"; return dir.length > 30 ? "…" + dir.slice(-27) : dir; }

function _toggleManualPollFields(show) {
  const el = document.getElementById("pref-manual-fields");
  if (el) el.style.display = show ? "block" : "none";
}
