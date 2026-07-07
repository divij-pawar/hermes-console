/**
 * util/prefs.js — localStorage preferences (appearance, panels, performance).
 */

const PREFS_KEY = "hermes-prefs";

/** Base poll intervals in ms (Normal preset). */
export const POLL_BASE = {
  status:   15000,
  usage:    10000,
  activity: 10000,
  traces:   15000,
  files:    30000,
  backup:   30000,
  cron:     30000,
  info:     30000,
  drawer:   10000,
  debugLog: 2000,
};

/** Preset multipliers for poll intervals. */
export const POLL_PRESETS = {
  normal: 1,
  eco:    2.5,
  manual: 1,
};

export const DEFAULT_PREFS = {
  dark: true,
  compact: false,
  pollPreset: "normal",
  pauseWhenHidden: true,
  pauseWhenDragging: true,
  sseBackoff: true,
  clientDebug: false,
  serverDebugLog: false,
  drawerAutoRefresh: true,
  drawerPollSec: 10,
  activityPollSec: 10,
  manualPollMultiplier: 1,
};

export function getPrefs() {
  try {
    return { ...DEFAULT_PREFS, ...JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

export function savePrefs(prefs) {
  localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
}

export function darkModeEnabled(prefs) {
  return (prefs || getPrefs()).dark !== false;
}

/** Effective multiplier applied to all poll intervals. */
export function pollMultiplier(prefs = getPrefs()) {
  if (prefs.pollPreset === "eco") return POLL_PRESETS.eco;
  if (prefs.pollPreset === "manual") {
    const m = Number(prefs.manualPollMultiplier);
    return Number.isFinite(m) && m > 0 ? m : 1;
  }
  return POLL_PRESETS.normal;
}

export function scaledPollMs(key, prefs = getPrefs()) {
  const base = POLL_BASE[key] ?? 10000;
  return Math.round(base * pollMultiplier(prefs));
}

export function activityPollMs(prefs = getPrefs()) {
  if (prefs.pollPreset === "manual") {
    const sec = Number(prefs.activityPollSec);
    return (Number.isFinite(sec) && sec > 0 ? sec : 10) * 1000;
  }
  return scaledPollMs("activity", prefs);
}

export function drawerPollMs(prefs = getPrefs()) {
  if (!prefs.drawerAutoRefresh) return 0;
  const sec = Number(prefs.drawerPollSec);
  return (Number.isFinite(sec) && sec > 0 ? sec : 10) * 1000;
}

const PANEL_SELECTORS = {
  usage:    "#usage-panel",
  backup:   "#backup-panel",
  traces:   "#prompt-trace-panel",
  activity: "#activity-panel",
  files:    "#files-panel",
  cron:     "#cron-panel",
};

/** True when panel is visible and not collapsed. */
export function isPanelActive(panelKey) {
  const prefs = getPrefs();
  if (prefs[`panel_${panelKey}`] === false) return false;
  const sel = PANEL_SELECTORS[panelKey];
  if (!sel) return true;
  const el = document.querySelector(sel);
  if (!el || el.style.display === "none") return false;
  if (el.classList.contains("panel-collapsed")) return false;
  if (panelKey === "usage" || panelKey === "traces" || panelKey === "activity") {
    if (document.body.classList.contains("right-collapsed")) return false;
  }
  if (panelKey === "backup" || panelKey === "files") {
    if (document.body.classList.contains("left-collapsed")) return false;
  }
  return true;
}
