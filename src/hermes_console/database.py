"""
hermes_console.database
~~~~~~~~~~~~~~~~~~~~~~~~
All access to monitor.db (the SQLite usage ledger written exclusively by this
process) is funnelled through this module.

Thread safety: every public function acquires ``monitor_db_lock`` before
opening a connection so HTTP worker threads and the Watcher thread never
contend on the same connection object.

After a successful insert, _insert_usage_event() calls broadcast() to push a
live usage_update to SSE clients.  The circular-import risk is avoided by
importing ``events`` lazily inside the function body.
"""

import hashlib
import json
import sqlite3
import threading
import time

from hermes_console.config import MONITOR_DB

monitor_db_lock = threading.Lock()


def _monitor_conn() -> sqlite3.Connection:
    """Open (and schema-ensure) the monitor.db connection.

    Must be called while holding *monitor_db_lock*.  The caller is responsible
    for closing the returned connection.
    """
    conn = sqlite3.connect(MONITOR_DB, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT UNIQUE,
            ts REAL NOT NULL,
            prompt_id TEXT,
            agent TEXT,
            provider TEXT NOT NULL,
            kind TEXT NOT NULL,
            model TEXT,
            query TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            latency_s REAL,
            estimated_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            usage_units INTEGER DEFAULT 1,
            session_id TEXT,
            detail TEXT,
            extra TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_ts ON usage_events(ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_provider ON usage_events(provider, ts DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_prompt ON usage_events(prompt_id, ts DESC)")
    return conn


def _usage_dedupe_key(*parts: object) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def _insert_usage_event(event: dict) -> None:
    """Insert one usage row into monitor.db and broadcast a live update.

    Broadcast happens *outside* the lock so the SSE push never blocks DB
    access.
    """
    payload = dict(event)
    payload.setdefault("ts", time.time())
    payload.setdefault("usage_units", 1)
    payload.setdefault("prompt_id", None)
    payload.setdefault("agent", None)
    payload.setdefault("model", None)
    payload.setdefault("query", None)
    payload.setdefault("input_tokens", 0)
    payload.setdefault("output_tokens", 0)
    payload.setdefault("cache_read_tokens", 0)
    payload.setdefault("cache_write_tokens", 0)
    payload.setdefault("estimated_cost_usd", None)
    payload.setdefault("cost_status", "")
    payload.setdefault("cost_source", "")
    payload.setdefault("latency_s", None)
    payload.setdefault("session_id", None)
    payload.setdefault("detail", None)
    payload.setdefault("extra", None)

    if not payload.get("dedupe_key"):
        payload["dedupe_key"] = _usage_dedupe_key(
            payload.get("provider"), payload.get("kind"), payload.get("agent"),
            payload.get("prompt_id"), payload.get("session_id"), payload.get("model"),
            payload.get("query"), payload.get("ts"),
        )

    if isinstance(payload.get("extra"), (dict, list)):
        payload["extra"] = json.dumps(payload["extra"], ensure_ascii=False, default=str)

    with monitor_db_lock:
        conn = _monitor_conn()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO usage_events
                (dedupe_key, ts, prompt_id, agent, provider, kind, model, query,
                 input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                 latency_s, estimated_cost_usd, cost_status, cost_source,
                 usage_units, session_id, detail, extra)
                VALUES
                (:dedupe_key, :ts, :prompt_id, :agent, :provider, :kind, :model, :query,
                 :input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens,
                 :latency_s, :estimated_cost_usd, :cost_status, :cost_source,
                 :usage_units, :session_id, :detail, :extra)
                """,
                payload,
            )
            conn.commit()
        finally:
            conn.close()

    # Broadcast outside lock to avoid holding it while the SSE queue is written.
    try:
        from hermes_console.events import broadcast
        from hermes_console.services.usage import usage_snapshot
        broadcast({"type": "usage_update", **usage_snapshot()})
    except Exception:
        pass


def _today_cutoff() -> float:
    """Unix timestamp for midnight (local) today — used in daily-aggregate queries."""
    import datetime as _dt
    now = _dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()
