/**
 * api/sse.js — Server-Sent Events connection management.
 *
 * Owns the EventSource lifecycle: connect, exponential-backoff reconnect,
 * event dispatch.  Delegates to a caller-supplied `onEvent(ev)` callback so
 * this module has zero knowledge of the UI.
 *
 * Usage:
 *   import { createSSEConnection } from './api/sse.js';
 *   const conn = createSSEConnection({
 *     url:         '/api/events',
 *     onEvent:     (ev) => dispatch(ev),
 *     onOpen:      () => setConnected(true),
 *     onClose:     () => setConnected(false),
 *     onReconnect: (delayMs) => showStatus(`reconnecting in ${delayMs}ms`),
 *   });
 *   conn.connect();
 *   conn.disconnect();
 */

const MIN_DELAY_MS = 3_000;
const MAX_DELAY_MS = 30_000;
const BACKOFF      = 1.5;

/**
 * @param {{
 *   url:         string,
 *   onEvent:     (ev: any) => void,
 *   onOpen?:     () => void,
 *   onClose?:    () => void,
 *   onReconnect?:(delayMs: number) => void,
 * }} options
 */
export function createSSEConnection({ url, onEvent, onOpen, onClose, onReconnect }) {
  let source      = null;
  let delay       = MIN_DELAY_MS;
  let reconnTimer = null;

  function connect() {
    if (source) {
      source.close();
      source = null;
    }
    clearTimeout(reconnTimer);

    source = new EventSource(url);

    source.onopen = () => {
      delay = MIN_DELAY_MS;
      onOpen?.();
    };

    source.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data);
        onEvent(ev);
      } catch {
        // ignore malformed frames
      }
    };

    source.onerror = () => {
      source.close();
      source = null;
      onClose?.();
      onReconnect?.(delay);
      reconnTimer = setTimeout(() => {
        delay = Math.min(delay * BACKOFF, MAX_DELAY_MS);
        connect();
      }, delay);
    };
  }

  function disconnect() {
    clearTimeout(reconnTimer);
    source?.close();
    source = null;
  }

  return { connect, disconnect };
}
