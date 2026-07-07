from __future__ import annotations

"""
hermes_console.services.prompt_trace
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Prompt trace overlay — tracks in-flight Slack/platform requests end-to-end.
This is observability only; it does not affect Kanban behaviour.
"""

import collections
import json
import time

from hermes_console.config import HERMES_ORCHESTRATOR
from hermes_console.database import (
    _monitor_conn,
    _usage_dedupe_key,
    monitor_db_lock,
)
from hermes_console.events import broadcast

PROMPT_TRACE_LIMIT = 30
active_prompt_trace:  dict[str, dict]         = {}
prompt_trace_history: collections.deque       = collections.deque(maxlen=PROMPT_TRACE_LIMIT)


def _trace_start(agent_id: str, msg: str, platform: str = "",
                 user: str = "", chat: str = "") -> str:
    trace_id = _usage_dedupe_key(agent_id, msg, time.time())[:12]
    trace = {
        "id":         trace_id,
        "agent":      agent_id,
        "platform":   platform,
        "user":       user,
        "chat":       chat,
        "msg":        (msg or "")[:500],
        "started_at": time.time(),
        "ended_at":   None,
        "status":     "running",
        "events":     [],
        "usage":      {"calls": 0, "input": 0, "output": 0, "cost": 0.0},
    }
    active_prompt_trace[agent_id] = trace
    broadcast({"type": "prompt_trace_update", "trace": trace})
    return trace_id


def _trace_add(agent_id: str, kind: str, detail: str = "", **extra) -> None:
    trace = active_prompt_trace.get(agent_id)
    if not trace:
        return
    event = {"ts": time.time(), "kind": kind, "detail": detail[:500] if detail else ""}
    event.update(extra)
    trace["events"].append(event)
    trace["events"] = trace["events"][-40:]
    broadcast({"type": "prompt_trace_update", "trace": trace})


def _trace_usage(agent_id: str, in_tok: int, out_tok: int, cost: float | None = None) -> None:
    trace = active_prompt_trace.get(agent_id)
    if not trace:
        return
    usage = trace.setdefault("usage", {"calls": 0, "input": 0, "output": 0, "cost": 0.0})
    usage["calls"]  = int(usage.get("calls") or 0) + 1
    usage["input"]  = int(usage.get("input") or 0) + int(in_tok or 0)
    usage["output"] = int(usage.get("output") or 0) + int(out_tok or 0)
    if cost is not None:
        usage["cost"] = float(usage.get("cost") or 0.0) + float(cost)
    broadcast({"type": "prompt_trace_update", "trace": trace})


def _trace_finish(agent_id: str, detail: str = "", **extra) -> None:
    trace = active_prompt_trace.pop(agent_id, None)
    if not trace:
        return
    trace["ended_at"] = time.time()
    trace["status"]   = "done"
    trace["final"]    = detail[:500] if detail else ""
    trace.update(extra)
    prompt_trace_history.append(trace)
    broadcast({"type": "prompt_trace_update", "trace": trace})


def _session_prompt_trace_fallback() -> list[dict]:
    """Fill prompt trace history from monitor.db session_rollup rows (for Slack
    sessions that completed before the server started)."""
    traces: list[dict] = []
    try:
        with monitor_db_lock:
            conn = _monitor_conn()
            rows = conn.execute(
                """
                SELECT dedupe_key, ts, prompt_id, agent, model, input_tokens, output_tokens,
                       cache_read_tokens, estimated_cost_usd, usage_units, detail, extra
                FROM usage_events
                WHERE provider = 'openrouter'
                  AND kind = 'session_rollup'
                ORDER BY ts DESC
                LIMIT 40
                """
            ).fetchall()
            conn.close()
    except Exception:
        return traces

    for r in rows:
        try:
            extra = json.loads(r["extra"] or "{}")
        except Exception:
            extra = {}
        if extra.get("source") != "slack":
            continue
        msg = r["detail"] or "Slack prompt"
        traces.append({
            "id":         f"session:{r['prompt_id']}",
            "agent":      r["agent"] or HERMES_ORCHESTRATOR,
            "platform":   "slack",
            "user":       "",
            "chat":       "",
            "msg":        msg,
            "started_at": r["ts"],
            "ended_at":   r["ts"],
            "status":     "session",
            "events": [{
                "ts":     r["ts"],
                "kind":   "session.rollup",
                "detail": f"{r['model']} · calls {r['usage_units'] or 0} · cache {r['cache_read_tokens'] or 0}",
            }],
            "usage": {
                "calls":  r["usage_units"] or 0,
                "input":  r["input_tokens"] or 0,
                "output": r["output_tokens"] or 0,
                "cost":   r["estimated_cost_usd"] or 0.0,
            },
        })
    return traces


def prompt_trace_snapshot() -> dict:
    traces = list(active_prompt_trace.values()) + list(prompt_trace_history)
    traces.extend(_session_prompt_trace_fallback())
    traces = sorted(traces, key=lambda t: t.get("started_at") or 0, reverse=True)
    deduped: list[dict] = []
    seen: set[str] = set()
    for trace in traces:
        tid = trace.get("id")
        if tid in seen:
            continue
        seen.add(tid)
        deduped.append(trace)
    return {"traces": deduped[:PROMPT_TRACE_LIMIT]}
