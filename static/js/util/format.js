/**
 * util/format.js — Pure formatting utilities.
 * No DOM access, no side effects.  Import anywhere.
 */

export function fmtNum(n) {
  return (n || 0).toLocaleString();
}

export function fmtMoney(n) {
  if (n == null || Number.isNaN(Number(n))) return "n/a";
  const v = Number(n);
  if (v === 0) return "$0.0000";
  if (Math.abs(v) < 0.01) return `$${v.toFixed(4)}`;
  return `$${v.toFixed(2)}`;
}

export function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Format a unix timestamp (seconds), epoch ms, or ISO string for display.
 *
 * opts.seconds — include seconds
 * opts.date    — include month + day
 * opts.tz      — include timezone abbreviation
 */
export function formatDisplayTime(value, opts = {}) {
  if (value == null || value === "") return "—";
  const d = _parseTimestamp(value);
  if (!d) return value ? String(value) : "—";
  const options = {
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    ...(opts.seconds ? { second: "2-digit" }  : {}),
    ...(opts.date    ? { month: "short", day: "numeric" } : {}),
    ...(opts.tz      ? { timeZoneName: "short" } : {}),
  };
  return new Intl.DateTimeFormat("en-US", options).format(d);
}

function _parseTimestamp(value) {
  if (value == null || value === "") return null;
  if (value instanceof Date) return isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    const ms = value < 1e12 ? value * 1000 : value;
    const d  = new Date(ms);
    return isNaN(d.getTime()) ? null : d;
  }
  const raw = String(value).trim();
  if (!raw) return null;
  if (/^\d+(\.\d+)?$/.test(raw)) return _parseTimestamp(Number(raw));
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(raw) ? raw.replace(" ", "T") : raw;
  const d = new Date(normalized);
  return isNaN(d.getTime()) ? null : d;
}

/** Short "H:MM AM/PM" format — alias for the no-opts form. */
export function fmtShortTime(ts) {
  return ts ? formatDisplayTime(ts) : "";
}

export function fmtElapsed(startedAt, endedAt) {
  if (!startedAt) return "—";
  const end = endedAt || Date.now() / 1000;
  const s   = Math.round(end - startedAt);
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
