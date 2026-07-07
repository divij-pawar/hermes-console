/**
 * util/debugLog.js — Client-side debug ring buffer + optional UI sink.
 */

import { getPrefs } from "./prefs.js";

const MAX_LINES = 400;
const _lines = [];

let _sink = null;

export function setDebugSink(el) {
  _sink = el;
  _paint();
}

function _enabled() {
  return !!getPrefs().clientDebug;
}

function _paint() {
  if (!_sink) return;
  _sink.textContent = _lines.join("\n") || "(empty — enable Client debug trace)";
  _sink.scrollTop = _sink.scrollHeight;
}

export function debugLog(category, message, data) {
  if (!_enabled()) return;
  const ts = new Date().toISOString().slice(11, 23);
  const extra = data != null ? ` ${typeof data === "string" ? data : JSON.stringify(data)}` : "";
  const line = `${ts} [${category}] ${message}${extra}`;
  _lines.push(line);
  if (_lines.length > MAX_LINES) _lines.splice(0, _lines.length - MAX_LINES);
  _paint();
}

export function clearDebugLog() {
  _lines.length = 0;
  _paint();
}

export function getDebugLines() {
  return [..._lines];
}

/** Wrap fetch to log slow requests when debug is on. */
export function instrumentFetch() {
  const orig = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input.url;
    const t0 = performance.now();
    try {
      const res = await orig(input, init);
      debugLog("fetch", `${init?.method || "GET"} ${url}`, `${res.status} ${Math.round(performance.now() - t0)}ms`);
      return res;
    } catch (e) {
      debugLog("fetch", `FAIL ${url}`, `${e.message} ${Math.round(performance.now() - t0)}ms`);
      throw e;
    }
  };
}
