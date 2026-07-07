from __future__ import annotations

"""
hermes_console.services.activity
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Recent activity-monitor rows from Postgres (tool_activity table).

Shows vector-memory calls, file reads, web searches with the agent that
performed them.  This is intentionally best-effort — if psycopg2 / Postgres
is unavailable the dashboard keeps working normally.
"""

import datetime as _dt
import time

from hermes_console.services.usage import _read_hermes_env_value

ACTIVITY_CACHE_TTL = 4.0
_activity_cache: dict = {"at": 0.0, "limit": 0, "payload": None}


def _iso_with_timezone(ts) -> str:
    if not hasattr(ts, "isoformat"):
        return str(ts or "")
    try:
        if ts.tzinfo is None:
            local_tz = _dt.datetime.now().astimezone().tzinfo
            ts = ts.replace(tzinfo=local_tz)
        return ts.isoformat()
    except Exception:
        return ts.isoformat()


def activity_snapshot(limit: int = 80) -> dict:
    """Query Postgres for recent tool_activity rows.

    Returns ``{"rows": [...], "counts": {...}}`` on success, or
    ``{"rows": [], "counts": {}, "error": "..."}`` on failure.
    """
    db_url = _read_hermes_env_value("HERMES_LOG_DB_URL")
    if not db_url:
        return {"rows": [], "counts": {}, "error": "HERMES_LOG_DB_URL not configured"}
    limit = max(1, min(int(limit or 80), 200))

    now = time.time()
    cached = _activity_cache.get("payload")
    if (
        cached is not None
        and _activity_cache.get("limit") == limit
        and now - float(_activity_cache.get("at") or 0) < ACTIVITY_CACHE_TTL
    ):
        return cached

    try:
        import psycopg2
        import psycopg2.extras
    except Exception as exc:
        return {
            "rows": [],
            "counts": {},
            "error": (
                f"psycopg2 unavailable: {exc}. "
                "Install with: python3 -m pip install --user psycopg2-binary "
                "(use the same python3 that runs server.py), then restart the console."
            ),
        }

    try:
        conn = psycopg2.connect(db_url)
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT id, ts, agent, tool, detail, session_id, duration_ms, extra
            FROM tool_activity
            WHERE tool LIKE 'memory:%%'
               OR tool = 'read_file'
               OR tool IN ('x:search', 'tavily:search', 'tavily:extract')
            ORDER BY ts DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows: list[dict]       = []
        counts: dict[str, int] = {}
        for r in cur.fetchall():
            tool  = r.get("tool") or ""
            counts[tool] = counts.get(tool, 0) + 1
            ts    = r.get("ts")
            extra = r.get("extra") or {}
            rows.append({
                "id":          r.get("id"),
                "ts":          _iso_with_timezone(ts),
                "agent":       r.get("agent") or "unknown",
                "tool":        tool,
                "detail":      r.get("detail") or "",
                "session_id":  r.get("session_id") or "",
                "duration_ms": r.get("duration_ms"),
                "extra":       extra,
            })
        conn.close()
        payload = {"rows": rows, "counts": counts}
        _activity_cache["at"] = now
        _activity_cache["limit"] = limit
        _activity_cache["payload"] = payload
        return payload
    except Exception as exc:
        return {"rows": [], "counts": {}, "error": str(exc)}
