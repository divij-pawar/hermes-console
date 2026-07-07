from __future__ import annotations

"""
hermes_console.services.usage
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Per-prompt token accumulation, OpenRouter key status, and the usage_snapshot()
that powers /api/usage and SSE ``usage_update`` events.
"""

import collections
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from hermes_console.config import (
    HERMES_DIR,
    HERMES_ORCHESTRATOR,
    AGENT_IDS,
    _KNOWN_AGENT_META,
    _AGENT_PALETTE,
    _AGENT_HOME_MAP,
    _agent_root,
)
from hermes_console.database import (
    _insert_usage_event,
    _usage_dedupe_key,
    _today_cutoff,
    _monitor_conn,
    monitor_db_lock,
)

# ── In-memory prompt accumulators ──────────────────────────────────────────────
PROMPT_HISTORY_LIMIT = 20
prompt_usage_lock = threading.Lock()
active_prompt: dict[str, dict] = {}
prompt_history: collections.deque = collections.deque(maxlen=PROMPT_HISTORY_LIMIT)

# ── OpenRouter key cache ────────────────────────────────────────────────────────
openrouter_key_state: dict = {
    "fetched_at": 0.0,
    "data": None,
    "error": None,
}
OPENROUTER_KEY_TTL = 60.0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_hermes_env_value(name: str) -> str | None:
    """Read *name* from environment, falling back to HERMES_DIR/.env."""
    value = os.environ.get(name)
    if value:
        return value
    env_path = os.path.join(HERMES_DIR, ".env")
    try:
        with open(env_path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key == name:
                    val = val.strip().strip("'\"")
                    return val or None
    except OSError:
        return None
    return None


def _estimate_openrouter_cost(model: str, prompt_tokens: int, completion_tokens: int,
                              cache_read_tokens: int = 0) -> dict:
    """Use Hermes pricing helpers when available; otherwise return tokens only."""
    try:
        hermes_agent_dir = os.path.join(HERMES_DIR, "hermes-agent")
        if hermes_agent_dir not in sys.path:
            sys.path.insert(0, hermes_agent_dir)
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
        billable_input = max(0, int(prompt_tokens or 0) - int(cache_read_tokens or 0))
        result = estimate_usage_cost(
            model,
            CanonicalUsage(
                input_tokens=billable_input,
                output_tokens=int(completion_tokens or 0),
                cache_read_tokens=int(cache_read_tokens or 0),
            ),
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key=_read_openrouter_api_key(),
        )
        amount = float(result.amount_usd) if result.amount_usd is not None else None
        return {
            "estimated_cost_usd": amount,
            "cost_status": result.status,
            "cost_source": result.source,
        }
    except Exception as exc:
        return {
            "estimated_cost_usd": None,
            "cost_status": "unknown",
            "cost_source": f"pricing_unavailable:{type(exc).__name__}",
        }


def _read_openrouter_api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_path = os.path.join(HERMES_DIR, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        return val
    except OSError:
        return None
    return None


def fetch_openrouter_key_status(force: bool = False) -> dict:
    """GET https://openrouter.ai/api/v1/key — returns credit + usage.

    Cached for OPENROUTER_KEY_TTL seconds to avoid hammering the API.
    """
    global openrouter_key_state
    now = time.time()
    if not force and openrouter_key_state["data"] is not None:
        if now - openrouter_key_state["fetched_at"] < OPENROUTER_KEY_TTL:
            return openrouter_key_state["data"]

    key = _read_openrouter_api_key()
    if not key:
        openrouter_key_state.update(fetched_at=now, data=None, error="no_key")
        return {"error": "OPENROUTER_API_KEY not configured"}

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            import json
            payload = json.loads(body).get("data") or {}
    except urllib.error.HTTPError as exc:
        openrouter_key_state.update(fetched_at=now, data=None, error=f"HTTP {exc.code}")
        return {"error": f"HTTP {exc.code} from OpenRouter"}
    except Exception as exc:
        openrouter_key_state.update(fetched_at=now, data=None, error=str(exc))
        return {"error": str(exc)}

    openrouter_key_state.update(fetched_at=now, data=payload, error=None)
    _check_openrouter_credits(payload)
    return payload


def _openrouter_key_remaining(payload: dict) -> float | None:
    raw = payload.get("limit_remaining")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    limit = payload.get("limit") or 0.0
    if limit <= 0:
        return None
    reset = str(payload.get("limit_reset") or "").strip().lower()
    if reset == "weekly":
        usage = payload.get("usage_weekly")
    elif reset == "daily":
        usage = payload.get("usage_daily")
    elif reset == "monthly":
        usage = payload.get("usage_monthly")
    else:
        usage = payload.get("usage")
    if usage is None:
        return None
    try:
        return float(limit) - float(usage)
    except (TypeError, ValueError):
        return None


def _check_openrouter_credits(payload: dict) -> None:
    from hermes_console.services.health import _raise_issue, _resolve_issue
    limit     = payload.get("limit") or 0.0
    remaining = _openrouter_key_remaining(payload)
    reset     = payload.get("limit_reset") or "period"

    if limit <= 0 or remaining is None:
        _resolve_issue("openrouter_no_credits")
        _resolve_issue("openrouter_low_credits")
        return

    if remaining <= 0:
        _raise_issue(
            "openrouter_no_credits", "critical", None,
            "OpenRouter API key limit reached",
            f"${remaining:.2f} remaining on this key's {reset} limit",
        )
    elif remaining < 1.0:
        _raise_issue(
            "openrouter_low_credits", "warning", None,
            f"OpenRouter key limit low — ${remaining:.2f} left",
            f"This key's {reset} spending cap is nearly exhausted",
        )
        _resolve_issue("openrouter_no_credits")
    else:
        _resolve_issue("openrouter_no_credits")
        _resolve_issue("openrouter_low_credits")


# ── Prompt accumulation ────────────────────────────────────────────────────────

def _start_prompt(agent_id: str, msg: str) -> None:
    """Begin a new per-prompt usage accumulator.  Rolls the previous active
    prompt into history if there was one."""
    with prompt_usage_lock:
        prev = active_prompt.get(agent_id)
        if prev:
            prev["ended_at"] = time.time()
            prompt_history.append(prev)
        prompt_id = _usage_dedupe_key(agent_id, msg, time.time())[:12]
        active_prompt[agent_id] = {
            "prompt_id":          prompt_id,
            "agent":              agent_id,
            "msg":                (msg or "")[:200],
            "started_at":         time.time(),
            "ended_at":           None,
            "calls":              0,
            "input":              0,
            "output":             0,
            "cache_hits":         0,
            "cache_reads":        0,
            "estimated_cost_usd": 0.0,
        }


def _credit_api_call(agent_id: str, in_tok: int, out_tok: int,
                     cache_hits: int = 0, cache_reads: int = 0,
                     *, model: str = "", provider: str = "openrouter",
                     latency_s: float | None = None, session_id: str = "",
                     call_no: str = "") -> None:
    with prompt_usage_lock:
        slot = active_prompt.get(agent_id)
        if slot is None:
            prompt_id = _usage_dedupe_key(agent_id, "autonomous", time.time())[:12]
            slot = {
                "prompt_id": prompt_id, "agent": agent_id,
                "msg": "(no inbound — autonomous turn)",
                "started_at": time.time(), "ended_at": None,
                "calls": 0, "input": 0, "output": 0,
                "cache_hits": 0, "cache_reads": 0, "estimated_cost_usd": 0.0,
            }
            active_prompt[agent_id] = slot
        slot["calls"]      += 1
        slot["input"]      += in_tok
        slot["output"]     += out_tok
        slot["cache_hits"] += cache_hits
        slot["cache_reads"] += cache_reads
        prompt_id = slot.get("prompt_id")

    cost   = _estimate_openrouter_cost(model, in_tok, out_tok, cache_hits)
    amount = cost.get("estimated_cost_usd")
    if amount is not None:
        with prompt_usage_lock:
            slot = active_prompt.get(agent_id)
            if slot and slot.get("prompt_id") == prompt_id:
                slot["estimated_cost_usd"] = float(slot.get("estimated_cost_usd") or 0.0) + float(amount)

    _insert_usage_event({
        "dedupe_key":          _usage_dedupe_key("openrouter", agent_id, session_id, call_no, model, in_tok, out_tok, cache_hits),
        "prompt_id":           prompt_id,
        "agent":               agent_id,
        "provider":            "openrouter",
        "kind":                "model_call",
        "model":               model,
        "input_tokens":        int(in_tok or 0),
        "output_tokens":       int(out_tok or 0),
        "cache_read_tokens":   int(cache_hits or 0),
        "latency_s":           latency_s,
        "estimated_cost_usd":  amount,
        "cost_status":         cost.get("cost_status", ""),
        "cost_source":         cost.get("cost_source", ""),
        "usage_units":         1,
        "session_id":          session_id,
        "detail":              f"{model} · {in_tok:,} in / {out_tok:,} out",
    })


def _credit_external_tool(provider: str, agent_id: str, kind: str, query: str = "",
                          *, session_id: str = "", duration_ms: int | None = None,
                          detail: str = "", extra: dict | None = None) -> None:
    normalized_provider = "x" if provider in ("x", "xai") else "tavily"
    prompt_id = ""
    with prompt_usage_lock:
        slot = active_prompt.get(agent_id)
        if slot:
            prompt_id = slot.get("prompt_id", "")
    _insert_usage_event({
        "dedupe_key": _usage_dedupe_key(normalized_provider, agent_id, kind, session_id, query, detail),
        "prompt_id":  prompt_id,
        "agent":      agent_id,
        "provider":   normalized_provider,
        "kind":       kind,
        "query":      query or detail,
        "latency_s":  (float(duration_ms) / 1000.0) if duration_ms else None,
        "usage_units": 1,
        "session_id": session_id,
        "detail":     detail or query,
        "extra":      extra or {},
    })


# ── Snapshots ──────────────────────────────────────────────────────────────────

def _active_providers() -> list[str]:
    configured: set[str] = set()
    _KEY_MAP = {
        "OPENROUTER_API_KEY": "openrouter",
        "XAI_API_KEY":        "x",
        "TAVILY_API_KEY":     "tavily",
    }
    for env_key, provider in _KEY_MAP.items():
        if _read_hermes_env_value(env_key):
            configured.add(provider)
    try:
        with monitor_db_lock:
            conn = _monitor_conn()
            rows = conn.execute("SELECT DISTINCT provider FROM usage_events").fetchall()
            conn.close()
        for r in rows:
            if r["provider"]:
                configured.add(r["provider"])
    except Exception:
        pass
    return [p for p in ("openrouter", "x", "tavily") if p in configured]


def _usage_provider_snapshot() -> dict:
    import json as _json
    cutoff    = _today_cutoff()
    providers = _active_providers()
    empty: dict = {}
    for p in providers:
        if p == "openrouter":
            empty[p] = {"calls_today": 0, "input_today": 0, "output_today": 0,
                        "cache_read_today": 0, "estimated_cost_today": 0.0, "recent": [], "models": []}
        else:
            empty[p] = {"calls_today": 0, "usage_units_today": 0, "recent": [], "failures_today": 0}
    try:
        with monitor_db_lock:
            conn = _monitor_conn()
            cur  = conn.cursor()
            for provider in providers:
                row = cur.execute(
                    """
                    SELECT COUNT(*) AS calls,
                           COALESCE(SUM(input_tokens),  0) AS input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                           COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost,
                           COALESCE(SUM(usage_units), 0) AS usage_units,
                           SUM(CASE WHEN kind LIKE '%error%' THEN 1 ELSE 0 END) AS failures
                    FROM usage_events
                    WHERE provider = ? AND ts >= ?
                    """,
                    (provider, cutoff),
                ).fetchone()
                recent_rows = cur.execute(
                    """
                    SELECT ts, agent, kind, model, query, input_tokens, output_tokens,
                           cache_read_tokens, latency_s, estimated_cost_usd, detail
                    FROM usage_events
                    WHERE provider = ?
                    ORDER BY ts DESC LIMIT 8
                    """,
                    (provider,),
                ).fetchall()
                recent = [
                    {
                        "ts": r["ts"], "agent": r["agent"] or "", "kind": r["kind"] or "",
                        "model": r["model"] or "", "query": r["query"] or "",
                        "input": r["input_tokens"] or 0, "output": r["output_tokens"] or 0,
                        "cache_read": r["cache_read_tokens"] or 0, "latency_s": r["latency_s"],
                        "estimated_cost_usd": r["estimated_cost_usd"], "detail": r["detail"] or "",
                    }
                    for r in recent_rows
                ]
                if provider == "openrouter":
                    models = cur.execute(
                        """
                        SELECT model, COUNT(*) AS calls,
                               COALESCE(SUM(input_tokens),  0) AS input_tokens,
                               COALESCE(SUM(output_tokens), 0) AS output_tokens,
                               COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost
                        FROM usage_events
                        WHERE provider = 'openrouter' AND ts >= ?
                        GROUP BY model
                        ORDER BY estimated_cost DESC, calls DESC
                        LIMIT 8
                        """,
                        (cutoff,),
                    ).fetchall()
                    empty["openrouter"] = {
                        "calls_today":          row["calls"] or 0,
                        "input_today":          row["input_tokens"] or 0,
                        "output_today":         row["output_tokens"] or 0,
                        "cache_read_today":     row["cache_read_tokens"] or 0,
                        "estimated_cost_today": float(row["estimated_cost"] or 0.0),
                        "recent":               recent,
                        "models":               [dict(m) for m in models],
                    }
                else:
                    empty[provider] = {
                        "calls_today":       row["calls"] or 0,
                        "usage_units_today": row["usage_units"] or 0,
                        "failures_today":    row["failures"] or 0,
                        "recent":            recent,
                    }
            conn.close()
    except Exception as exc:
        empty["error"] = str(exc)
    return empty


def _agent_metadata_list() -> list[dict]:
    """Return metadata for every discovered agent — consumed by /api/agents."""
    result      = []
    palette_idx = 0
    for aid in AGENT_IDS:
        meta = _KNOWN_AGENT_META.get(aid)
        if meta:
            color = meta["color"]
            emoji = meta["emoji"]
            label = meta["label"]
        else:
            color, emoji = _AGENT_PALETTE[palette_idx % len(_AGENT_PALETTE)]
            label = aid.upper()
            palette_idx += 1
        result.append({
            "id":       aid,
            "label":    label,
            "emoji":    emoji,
            "color":    color,
            "root":     _agent_root(aid),
            "is_extra": aid in _AGENT_HOME_MAP,
        })
    return result


def usage_snapshot() -> dict:
    """Snapshot used by /api/usage and SSE usage_update events."""
    with prompt_usage_lock:
        active  = {aid: dict(s) for aid, s in active_prompt.items()}
        history = list(prompt_history)
    usage = _usage_provider_snapshot()
    return {
        "active":    active,
        "history":   history,
        "providers": usage,
        "openrouter": fetch_openrouter_key_status(),
    }
