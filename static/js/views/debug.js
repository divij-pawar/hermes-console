/**
 * views/debug.js — Client trace sink + server console.log tail viewer.
 */

import * as api from "../api/client.js";
import { getPrefs } from "../util/prefs.js";
import { setDebugSink, clearDebugLog } from "../util/debugLog.js";

export function initDebugPanel() {
  const clientEl = document.getElementById("debug-client-log");
  if (clientEl) setDebugSink(clientEl);
}

export async function refreshServerDebugLog() {
  if (!getPrefs().serverDebugLog) return;
  const el = document.getElementById("debug-server-log");
  if (!el) return;
  try {
    const data = await api.fetchConsoleLog(200);
    const text = (data.lines || []).join("\n");
    el.textContent = text || data.error || "(empty)";
    el.scrollTop = el.scrollHeight;
  } catch (e) {
    el.textContent = `Failed to load console.log: ${e.message}`;
  }
}

export function clearClientDebugLogView() {
  clearDebugLog();
}
