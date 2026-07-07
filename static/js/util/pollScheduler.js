/**
 * util/pollScheduler.js — Visibility/panel/SSE-aware polling scheduler.
 */

import { getPrefs, scaledPollMs, activityPollMs, drawerPollMs, isPanelActive } from "./prefs.js";
import { store } from "../state/store.js";
import { debugLog } from "./debugLog.js";

/** @typedef {{ id: string, fn: () => void|Promise<void>, baseKey?: string, panelKey?: string, sseBackoff?: boolean, always?: boolean }} PollJob */

const _jobs = new Map();
let _timer = null;

export function setUiDragging(active) {
  store.uiDragging = active;
}

export function setSseOnline(online) {
  store.sseOnline = online;
}

function _effectiveInterval(job) {
  const prefs = getPrefs();
  if (prefs.pauseWhenHidden && document.hidden) return 0;
  if (prefs.pauseWhenDragging && store.uiDragging) return 0;

  if (job.panelKey && !isPanelActive(job.panelKey)) return 0;

  if (job.sseBackoff !== false && prefs.sseBackoff && store.sseOnline) {
    const slow = scaledPollMs(job.baseKey || "status", prefs) * 3;
    return slow;
  }

  if (job.baseKey === "activity") return activityPollMs(prefs);
  if (job.baseKey === "drawer") {
    const ms = drawerPollMs(prefs);
    if (!ms || !store.openCardId) return 0;
    return ms;
  }
  if (job.baseKey) return scaledPollMs(job.baseKey, prefs);
  return 10000;
}

function _tick() {
  const now = Date.now();
  for (const job of _jobs.values()) {
    const interval = job.always ? (_effectiveInterval(job) || job.fallbackMs || 30000) : _effectiveInterval(job);
    if (!interval) continue;
    if (!job.lastRun) job.lastRun = 0;
    if (now - job.lastRun < interval) continue;
    job.lastRun = now;
    debugLog("poll", job.id);
    Promise.resolve(job.fn()).catch(() => {});
  }
}

function _ensureTimer() {
  if (_timer) return;
  _timer = setInterval(_tick, 500);
}

export function registerPoll(job) {
  _jobs.set(job.id, { lastRun: 0, ...job });
  _ensureTimer();
}

export function unregisterPoll(id) {
  _jobs.delete(id);
}

export function runPollNow(id) {
  const job = _jobs.get(id);
  if (!job) return;
  job.lastRun = 0;
  _tick();
}

export function initPollVisibility() {
  document.addEventListener("visibilitychange", () => {
    debugLog("ui", document.hidden ? "tab hidden" : "tab visible");
    if (!document.hidden) _tick();
  });
}
