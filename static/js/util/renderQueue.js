/**
 * util/renderQueue.js — Debounced renders and skip-if-unchanged helpers.
 */

const _pending = new Map();
const _fingerprints = new Map();

export function fingerprint(data) {
  try {
    return JSON.stringify(data);
  } catch {
    return String(Date.now());
  }
}

/** Skip render when serialized payload matches previous. Returns true if skipped. */
export function skipIfUnchanged(key, data) {
  const fp = fingerprint(data);
  if (_fingerprints.get(key) === fp) return true;
  _fingerprints.set(key, fp);
  return false;
}

export function clearFingerprint(key) {
  _fingerprints.delete(key);
}

/** Coalesce rapid updates into one rAF callback per key. */
export function scheduleRender(key, fn) {
  if (_pending.has(key)) cancelAnimationFrame(_pending.get(key));
  _pending.set(key, requestAnimationFrame(() => {
    _pending.delete(key);
    fn();
  }));
}

/** Pause coalesced renders while dragging/resizing (checked by caller). */
export function flushRender(key, fn) {
  if (_pending.has(key)) {
    cancelAnimationFrame(_pending.get(key));
    _pending.delete(key);
  }
  fn();
}
