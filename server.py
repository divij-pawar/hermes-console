#!/usr/bin/env python3
"""Hermes Web UI - Console Server
Serves a control panel dashboard for Hermes agent activity (Sage, Imagine, future
specialists). Watches per-profile session jsonl files, output directories, and
the shared gateway log. Uses only Python stdlib — no pip deps required.

Start: python3 server.py
"""

import collections
import glob
import hashlib
import re
import http.server
import json
import os
import queue
import signal
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path


# ── .env bootstrap ─────────────────────────────────────────────────────────────
# Load .env (and .env.local for local overrides) from the web-ui directory
# before reading any os.environ.get() calls. Shell environment always wins —
# values already set in the environment are never overwritten.
def _bootstrap_env() -> None:
    _dir = os.path.dirname(os.path.abspath(__file__))
    for _name in (".env", ".env.local"):
        _path = os.path.join(_dir, _name)
        try:
            with open(_path, encoding="utf-8") as _f:
                for _raw in _f:
                    _line = _raw.strip()
                    if not _line or _line.startswith("#") or "=" not in _line:
                        continue
                    _key, _val = _line.split("=", 1)
                    _key = _key.strip()
                    _val = _val.strip().strip("'\"")
                    if _key and _key not in os.environ:
                        os.environ[_key] = _val
        except OSError:
            pass

_bootstrap_env()

# ── Paths ──────────────────────────────────────────────────────────────────────
HERMES_DIR         = os.environ.get("HERMES_DIR", os.path.expanduser("~/.hermes"))
HERMES_ORCHESTRATOR = os.environ.get("HERMES_ORCHESTRATOR", "sage")
PROFILES_DIR  = os.path.join(HERMES_DIR, "profiles")
WORKSPACE_DIR = os.path.join(HERMES_DIR, "workspace")
LOGS_DIR      = os.path.join(HERMES_DIR, "logs")
KANBAN_DB     = os.path.join(HERMES_DIR, "kanban.db")
KANBAN_LOGS   = os.path.join(HERMES_DIR, "kanban", "logs")
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
MONITOR_DB    = os.environ.get("HERMES_MONITOR_DB", os.path.join(SCRIPT_DIR, "monitor.db"))
_DEFAULT_BACKUP_REPO = ""  # no hardcoded fallback — only resolve if configured in HERMES_DIR
_SF_AGENTS_REPO_FILE = os.path.join(HERMES_DIR, ".sf_agents_repo")


def _read_sf_agents_repo_file() -> str:
    try:
        with open(_SF_AGENTS_REPO_FILE, encoding="utf-8") as f:
            path = f.read().strip()
        if path and os.path.isdir(path):
            return path
    except OSError:
        pass
    return ""


def _resolve_backup_paths() -> tuple[str, str]:
    """Return (repo_dir, script_path); empty strings when backup is unavailable."""
    repo = (
        os.environ.get("HERMES_BACKUP_REPO", "").strip()
        or os.environ.get("SF_AGENTS_REPO", "").strip()
        or _read_sf_agents_repo_file()
    )
    if not repo and os.path.isdir(_DEFAULT_BACKUP_REPO):
        repo = _DEFAULT_BACKUP_REPO
    script = os.environ.get("HERMES_BACKUP_SCRIPT", "").strip()
    if not script and repo:
        candidate = os.path.join(repo, "backup.sh")
        if os.path.isfile(candidate):
            script = candidate
    return repo, script


BACKUP_REPO, BACKUP_SCRIPT = _resolve_backup_paths()
# ── Agent metadata & discovery ─────────────────────────────────────────────

# Well-known agents get fixed colors and emojis. Any agent discovered in
# profiles/ but not listed here gets the next entry from _AGENT_PALETTE.
_KNOWN_AGENT_META: dict[str, dict] = {
    "sage":    {"emoji": "🧭", "color": "#58a6ff", "label": "SAGE"},
    "imagine": {"emoji": "🎨", "color": "#bc8cff", "label": "IMAGINE"},
    "ink":     {"emoji": "🖊️",  "color": "#3fb950", "label": "INK"},
    "recon":   {"emoji": "🔎", "color": "#f0883e", "label": "RECON"},
    "signal":  {"emoji": "📡", "color": "#79c0ff", "label": "SIGNAL"},
    "anton":   {"emoji": "🔨", "color": "#e3b341", "label": "ANTON"},
}
_AGENT_PALETTE = [
    ("#a5d6ff", "⚡"), ("#f778ba", "🔬"), ("#56d364", "🎯"),
    ("#ff7b72", "💡"), ("#ffa657", "🛠️"), ("#d2a8ff", "🌐"),
]


def _discover_agents(hermes_dir: str) -> list[str]:
    """Return agent IDs present in *hermes_dir*.

    Always includes the orchestrator (HERMES_ORCHESTRATOR, default "sage") as
    the default-profile root. Any subdirectory found under profiles/ is added.
    """
    agents = [HERMES_ORCHESTRATOR]
    profiles = os.path.join(hermes_dir, "profiles")
    if os.path.isdir(profiles):
        for name in sorted(os.listdir(profiles)):
            if os.path.isdir(os.path.join(profiles, name)) and name not in agents:
                agents.append(name)
    return agents


def _agent_log_paths(agent_id: str, hermes_dir: str) -> dict[str, str]:
    """Return {gateway: path, agent: path} for *agent_id* inside *hermes_dir*."""
    if agent_id == HERMES_ORCHESTRATOR:
        logs = os.path.join(hermes_dir, "logs")
    else:
        logs = os.path.join(hermes_dir, "profiles", agent_id, "logs")
    return {
        "gateway": os.path.join(logs, "gateway.log"),
        "agent":   os.path.join(logs, "agent.log"),
    }


# ── Primary + extra Hermes homes ───────────────────────────────────────────

# _AGENT_HOME_MAP overrides which hermes_dir to use for agents sourced from
# HERMES_EXTRA_HOME (e.g. the Docker lab bind-mount on the host).
_AGENT_HOME_MAP: dict[str, str] = {}

AGENT_IDS: list[str] = _discover_agents(HERMES_DIR)

# Set HERMES_EXTRA_HOME to watch a second Hermes home alongside the primary.
# Example: HERMES_EXTRA_HOME=/tmp/hermes-anton-lab/home
HERMES_EXTRA_HOME = os.environ.get("HERMES_EXTRA_HOME", "").strip()
if HERMES_EXTRA_HOME and os.path.isdir(HERMES_EXTRA_HOME):
    for _eid in _discover_agents(HERMES_EXTRA_HOME):
        if _eid not in AGENT_IDS:          # never shadow a primary-home agent
            AGENT_IDS.append(_eid)
            _AGENT_HOME_MAP[_eid] = HERMES_EXTRA_HOME


def _agent_home(agent_id: str) -> str:
    """Return the Hermes home directory for *agent_id*."""
    return _AGENT_HOME_MAP.get(agent_id, HERMES_DIR)


def _agent_root(agent_id: str) -> str:
    """Return the profile root directory for *agent_id*."""
    home = _agent_home(agent_id)
    if agent_id == HERMES_ORCHESTRATOR and home == HERMES_DIR:
        return HERMES_DIR
    if agent_id == HERMES_ORCHESTRATOR:
        return home
    return os.path.join(home, "profiles", agent_id)


# Per-agent live log sources. Built from AGENT_IDS so any newly discovered
# profile is included automatically — no manual edits needed.
AGENT_LIVE_LOGS: dict[str, dict[str, str]] = {
    aid: _agent_log_paths(aid, _agent_home(aid))
    for aid in AGENT_IDS
}

# Alias for the primary gateway log — keyed off the configured orchestrator.
GATEWAY_LOG = AGENT_LIVE_LOGS[HERMES_ORCHESTRATOR]["gateway"]

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT       = int(os.environ.get("PORT") or os.environ.get("HERMES_WEBUI_PORT", "7979"))


def _agent_output_dir(agent_id: str) -> str:
    home = _agent_home(agent_id)
    return os.path.join(home, "workspace", agent_id, "output")


AGENT_OUTPUT_DIRS: dict[str, str] = {
    aid: _agent_output_dir(aid)
    for aid in AGENT_IDS
}

# ── Global state ───────────────────────────────────────────────────────────────
sse_clients: list[queue.Queue] = []
sse_lock = threading.Lock()

agent_state: dict = {
    aid: {"active": False, "last_seen": None, "session": None}
    for aid in AGENT_IDS
}


seen_events: set = set()  # (agent_id, event_id)

# Per-prompt token accumulation. A "prompt" begins when an inbound message
# arrives for a given agent and ends when the next inbound arrives. All
# API-call events between are credited to it. We keep the active prompt
# plus the last N completed prompts as history.
PROMPT_HISTORY_LIMIT = 20
prompt_usage_lock = threading.Lock()
active_prompt: dict[str, dict] = {}      # agent_id -> {start_ts, msg, calls, input, output, cache_hits}
prompt_history: collections.deque = collections.deque(maxlen=PROMPT_HISTORY_LIMIT)

monitor_db_lock = threading.Lock()

# Slack prompt trace state. This is an observability overlay only; it does not
# change Kanban behavior.
PROMPT_TRACE_LIMIT = 30
active_prompt_trace: dict[str, dict] = {}
prompt_trace_history: collections.deque = collections.deque(maxlen=PROMPT_TRACE_LIMIT)

# OpenRouter key info — refreshed at most every 60s.
openrouter_key_state: dict = {
    "fetched_at": 0.0,
    "data": None,
    "error": None,
}
OPENROUTER_KEY_TTL = 60.0  # seconds


def _monitor_conn() -> sqlite3.Connection:
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
    try:
        broadcast({"type": "usage_update", **usage_snapshot()})
    except Exception:
        pass


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
        "prompt_id": prompt_id,
        "agent": agent_id,
        "provider": normalized_provider,
        "kind": kind,
        "query": query or detail,
        "latency_s": (float(duration_ms) / 1000.0) if duration_ms else None,
        "usage_units": 1,
        "session_id": session_id,
        "detail": detail or query,
        "extra": extra or {},
    })


def _trace_start(agent_id: str, msg: str, platform: str = "", user: str = "", chat: str = "") -> str:
    trace_id = _usage_dedupe_key(agent_id, msg, time.time())[:12]
    trace = {
        "id": trace_id,
        "agent": agent_id,
        "platform": platform,
        "user": user,
        "chat": chat,
        "msg": (msg or "")[:500],
        "started_at": time.time(),
        "ended_at": None,
        "status": "running",
        "events": [],
        "usage": {"calls": 0, "input": 0, "output": 0, "cost": 0.0},
    }
    active_prompt_trace[agent_id] = trace
    broadcast({"type": "prompt_trace_update", "trace": trace})
    return trace_id


def _trace_add(agent_id: str, kind: str, detail: str = "", **extra) -> None:
    trace = active_prompt_trace.get(agent_id)
    if not trace:
        return
    event = {
        "ts": time.time(),
        "kind": kind,
        "detail": detail[:500] if detail else "",
    }
    event.update(extra)
    trace["events"].append(event)
    trace["events"] = trace["events"][-40:]
    broadcast({"type": "prompt_trace_update", "trace": trace})


def _trace_usage(agent_id: str, in_tok: int, out_tok: int, cost: float | None = None) -> None:
    trace = active_prompt_trace.get(agent_id)
    if not trace:
        return
    usage = trace.setdefault("usage", {"calls": 0, "input": 0, "output": 0, "cost": 0.0})
    usage["calls"] = int(usage.get("calls") or 0) + 1
    usage["input"] = int(usage.get("input") or 0) + int(in_tok or 0)
    usage["output"] = int(usage.get("output") or 0) + int(out_tok or 0)
    if cost is not None:
        usage["cost"] = float(usage.get("cost") or 0.0) + float(cost)
    broadcast({"type": "prompt_trace_update", "trace": trace})


def _trace_finish(agent_id: str, detail: str = "", **extra) -> None:
    trace = active_prompt_trace.pop(agent_id, None)
    if not trace:
        return
    trace["ended_at"] = time.time()
    trace["status"] = "done"
    trace["final"] = detail[:500] if detail else ""
    trace.update(extra)
    prompt_trace_history.append(trace)
    broadcast({"type": "prompt_trace_update", "trace": trace})


def prompt_trace_snapshot() -> dict:
    traces = list(active_prompt_trace.values()) + list(prompt_trace_history)
    traces.extend(_session_prompt_trace_fallback())
    traces = sorted(traces, key=lambda t: t.get("started_at") or 0, reverse=True)
    deduped = []
    seen = set()
    for trace in traces:
        tid = trace.get("id")
        if tid in seen:
            continue
        seen.add(tid)
        deduped.append(trace)
    return {"traces": deduped[:PROMPT_TRACE_LIMIT]}


def _session_prompt_trace_fallback() -> list[dict]:
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
            "id": f"session:{r['prompt_id']}",
            "agent": r["agent"] or HERMES_ORCHESTRATOR,
            "platform": "slack",
            "user": "",
            "chat": "",
            "msg": msg,
            "started_at": r["ts"],
            "ended_at": r["ts"],
            "status": "session",
            "events": [{
                "ts": r["ts"],
                "kind": "session.rollup",
                "detail": f"{r['model']} · calls {r['usage_units'] or 0} · cache {r['cache_read_tokens'] or 0}",
            }],
            "usage": {
                "calls": r["usage_units"] or 0,
                "input": r["input_tokens"] or 0,
                "output": r["output_tokens"] or 0,
                "cost": r["estimated_cost_usd"] or 0.0,
            },
        })
    return traces


def _today_cutoff() -> float:
    import datetime as _dt
    now = _dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp()


# ── Health / Issue Tracker ─────────────────────────────────────────────────────
# issues_state maps a stable issue_id → issue dict. Issues are raised
# immediately when detected (log line, balance check, kanban state) and
# resolved as soon as the condition clears. Every change is pushed over SSE
# so the dashboard updates within one poll cycle (<100 ms).

issues_state: dict[str, dict] = {}
issues_lock = threading.Lock()


def _raise_issue(issue_id: str, severity: str, agent: str | None,
                 title: str, detail: str = "") -> None:
    """Register or refresh an active issue and broadcast immediately.

    severity: "critical" | "warning" | "info"
    Suppresses re-broadcast if title+detail are unchanged (avoids log spam).
    """
    now = _now_hms()
    issue = {
        "id": issue_id,
        "severity": severity,
        "agent": agent or "",
        "title": title,
        "detail": detail,
        "ts": now,
    }
    with issues_lock:
        existing = issues_state.get(issue_id)
        if (existing
                and existing.get("title") == title
                and existing.get("detail") == detail):
            return
        issues_state[issue_id] = issue
    broadcast({"type": "health_alert", "action": "raise", "issue": issue})


def _resolve_issue(issue_id: str) -> None:
    with issues_lock:
        if issue_id not in issues_state:
            return
        issues_state.pop(issue_id, None)
    broadcast({"type": "health_alert", "action": "resolve", "issue_id": issue_id})


def _resolve_prefix(prefix: str) -> None:
    """Resolve all issues whose id starts with `prefix`."""
    with issues_lock:
        to_remove = [k for k in list(issues_state) if k.startswith(prefix)]
        for k in to_remove:
            issues_state.pop(k, None)
    for k in to_remove:
        broadcast({"type": "health_alert", "action": "resolve", "issue_id": k})


def _get_issues() -> list[dict]:
    with issues_lock:
        return sorted(issues_state.values(),
                      key=lambda x: ("critical" != x["severity"], x["ts"]),
                      reverse=False)


def _read_openrouter_api_key() -> str | None:
    """Pull OPENROUTER_API_KEY from the default profile's .env (the same
    file the gateway reads). Doesn't import dotenv — we parse manually so
    the web-ui process inherits no extra deps.
    """
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
    """GET https://openrouter.ai/api/v1/key — returns credit + usage."""
    global openrouter_key_state
    now = time.time()
    if not force and openrouter_key_state["data"] is not None:
        if now - openrouter_key_state["fetched_at"] < OPENROUTER_KEY_TTL:
            return openrouter_key_state["data"]

    key = _read_openrouter_api_key()
    if not key:
        openrouter_key_state.update(fetched_at=now, data=None, error="no_key")
        return {"error": "OPENROUTER_API_KEY not configured"}

    import urllib.request, urllib.error
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(body).get("data") or {}
    except urllib.error.HTTPError as exc:
        openrouter_key_state.update(fetched_at=now, data=None, error=f"HTTP {exc.code}")
        return {"error": f"HTTP {exc.code} from OpenRouter"}
    except Exception as exc:
        openrouter_key_state.update(fetched_at=now, data=None, error=str(exc))
        return {"error": str(exc)}

    openrouter_key_state.update(fetched_at=now, data=payload, error=None)

    # Side-effect: raise/resolve credit issues immediately on fresh data.
    _check_openrouter_credits(payload)

    return payload


def _openrouter_key_remaining(payload: dict) -> float | None:
    """Return remaining spend for the current key limit window, if capped."""
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
    """Raise/resolve credit-related issues based on the /api/v1/key payload."""
    limit = payload.get("limit") or 0.0
    remaining = _openrouter_key_remaining(payload)
    reset = payload.get("limit_reset") or "period"

    # limit=0 means no per-key cap configured.
    if limit <= 0 or remaining is None:
        _resolve_issue("openrouter_no_credits")
        _resolve_issue("openrouter_low_credits")
        return

    if remaining <= 0:
        _raise_issue(
            "openrouter_no_credits",
            "critical",
            None,
            "OpenRouter API key limit reached",
            f"${remaining:.2f} remaining on this key's {reset} limit — "
            "raise the key limit in OpenRouter, not just account credits",
        )
    elif remaining < 1.0:
        _raise_issue(
            "openrouter_low_credits",
            "warning",
            None,
            f"OpenRouter key limit low — ${remaining:.2f} left",
            f"This key's {reset} spending cap is nearly exhausted",
        )
        _resolve_issue("openrouter_no_credits")
    else:
        _resolve_issue("openrouter_no_credits")
        _resolve_issue("openrouter_low_credits")


def _start_prompt(agent_id: str, msg: str):
    """Begin a new per-prompt usage accumulator for `agent_id`. Rolls the
    previous active prompt into history if there was one."""
    with prompt_usage_lock:
        prev = active_prompt.get(agent_id)
        if prev:
            prev["ended_at"] = time.time()
            prompt_history.append(prev)
        prompt_id = _usage_dedupe_key(agent_id, msg, time.time())[:12]
        active_prompt[agent_id] = {
            "prompt_id": prompt_id,
            "agent": agent_id,
            "msg": (msg or "")[:200],
            "started_at": time.time(),
            "ended_at": None,
            "calls": 0,
            "input": 0,
            "output": 0,
            "cache_hits": 0,
            "cache_reads": 0,
            "estimated_cost_usd": 0.0,
        }


def _credit_api_call(agent_id: str, in_tok: int, out_tok: int,
                     cache_hits: int = 0, cache_reads: int = 0,
                     *, model: str = "", provider: str = "openrouter",
                     latency_s: float | None = None, session_id: str = "",
                     call_no: str = ""):
    """Credit one API call to the currently-active prompt for `agent_id`.
    If there's no active prompt yet (e.g. an autonomous run), create a
    synthetic 'idle' prompt entry so the tokens are still tracked."""
    with prompt_usage_lock:
        slot = active_prompt.get(agent_id)
        if slot is None:
            prompt_id = _usage_dedupe_key(agent_id, "autonomous", time.time())[:12]
            slot = {
                "prompt_id": prompt_id,
                "agent": agent_id,
                "msg": "(no inbound — autonomous turn)",
                "started_at": time.time(),
                "ended_at": None,
                "calls": 0,
                "input": 0,
                "output": 0,
                "cache_hits": 0,
                "cache_reads": 0,
                "estimated_cost_usd": 0.0,
            }
            active_prompt[agent_id] = slot
        slot["calls"] += 1
        slot["input"] += in_tok
        slot["output"] += out_tok
        slot["cache_hits"] += cache_hits
        slot["cache_reads"] += cache_reads
        prompt_id = slot.get("prompt_id")

    cost = _estimate_openrouter_cost(model, in_tok, out_tok, cache_hits)
    amount = cost.get("estimated_cost_usd")
    if amount is not None:
        with prompt_usage_lock:
            slot = active_prompt.get(agent_id)
            if slot and slot.get("prompt_id") == prompt_id:
                slot["estimated_cost_usd"] = float(slot.get("estimated_cost_usd") or 0.0) + float(amount)

    _insert_usage_event({
        "dedupe_key": _usage_dedupe_key("openrouter", agent_id, session_id, call_no, model, in_tok, out_tok, cache_hits),
        "prompt_id": prompt_id,
        "agent": agent_id,
        "provider": "openrouter",
        "kind": "model_call",
        "model": model,
        "input_tokens": int(in_tok or 0),
        "output_tokens": int(out_tok or 0),
        "cache_read_tokens": int(cache_hits or 0),
        "latency_s": latency_s,
        "estimated_cost_usd": amount,
        "cost_status": cost.get("cost_status", ""),
        "cost_source": cost.get("cost_source", ""),
        "usage_units": 1,
        "session_id": session_id,
        "detail": f"{model} · {in_tok:,} in / {out_tok:,} out",
    })


def _agent_metadata_list() -> list[dict]:
    """Return metadata for every discovered agent — consumed by /api/agents.

    Well-known agents get fixed colors/emojis from _KNOWN_AGENT_META. Newly
    discovered agents get the next slot from _AGENT_PALETTE so the UI never
    renders a plain grey dot for an unknown worker.
    """
    result = []
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
    """Snapshot used by /api/usage."""
    with prompt_usage_lock:
        active = {aid: dict(s) for aid, s in active_prompt.items()}
        history = list(prompt_history)
    usage = _usage_provider_snapshot()
    return {
        "active": active,
        "history": history,
        "providers": usage,
        "openrouter": fetch_openrouter_key_status(),
    }


def _active_providers() -> list[str]:
    """Return provider names that are configured or have historical events in monitor.db."""
    configured = set()
    _KEY_MAP = {
        "OPENROUTER_API_KEY": "openrouter",
        "XAI_API_KEY": "x",
        "TAVILY_API_KEY": "tavily",
    }
    for env_key, provider in _KEY_MAP.items():
        if _read_hermes_env_value(env_key):
            configured.add(provider)
    # Also include any provider that already has events recorded (don't hide history)
    try:
        with monitor_db_lock:
            conn = _monitor_conn()
            rows = conn.execute(
                "SELECT DISTINCT provider FROM usage_events"
            ).fetchall()
            conn.close()
        for r in rows:
            if r["provider"]:
                configured.add(r["provider"])
    except Exception:
        pass
    # Preserve a stable display order
    return [p for p in ("openrouter", "x", "tavily") if p in configured]


def _usage_provider_snapshot() -> dict:
    cutoff = _today_cutoff()
    providers = _active_providers()
    empty: dict = {}
    for p in providers:
        if p == "openrouter":
            empty[p] = {"calls_today": 0, "input_today": 0, "output_today": 0, "cache_read_today": 0, "estimated_cost_today": 0.0, "recent": [], "models": []}
        else:
            empty[p] = {"calls_today": 0, "usage_units_today": 0, "recent": [], "failures_today": 0}
    try:
        with monitor_db_lock:
            conn = _monitor_conn()
            cur = conn.cursor()
            for provider in providers:
                row = cur.execute(
                    """
                    SELECT COUNT(*) AS calls,
                           COALESCE(SUM(input_tokens), 0) AS input_tokens,
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
                    ORDER BY ts DESC
                    LIMIT 8
                    """,
                    (provider,),
                ).fetchall()
                recent = []
                for r in recent_rows:
                    recent.append({
                        "ts": r["ts"],
                        "agent": r["agent"] or "",
                        "kind": r["kind"] or "",
                        "model": r["model"] or "",
                        "query": r["query"] or "",
                        "input": r["input_tokens"] or 0,
                        "output": r["output_tokens"] or 0,
                        "cache_read": r["cache_read_tokens"] or 0,
                        "latency_s": r["latency_s"],
                        "estimated_cost_usd": r["estimated_cost_usd"],
                        "detail": r["detail"] or "",
                    })
                if provider == "openrouter":
                    models = cur.execute(
                        """
                        SELECT model, COUNT(*) AS calls,
                               COALESCE(SUM(input_tokens), 0) AS input_tokens,
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
                        "calls_today": row["calls"] or 0,
                        "input_today": row["input_tokens"] or 0,
                        "output_today": row["output_tokens"] or 0,
                        "cache_read_today": row["cache_read_tokens"] or 0,
                        "estimated_cost_today": float(row["estimated_cost"] or 0.0),
                        "recent": recent,
                        "models": [dict(m) for m in models],
                    }
                else:
                    empty[provider] = {
                        "calls_today": row["calls"] or 0,
                        "usage_units_today": row["usage_units"] or 0,
                        "failures_today": row["failures"] or 0,
                        "recent": recent,
                    }
            conn.close()
    except Exception as exc:
        empty["error"] = str(exc)
    return empty


def _read_hermes_env_value(name: str) -> str | None:
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


def activity_snapshot(limit: int = 80) -> dict:
    """Recent activity-monitor rows from Postgres.

    Shows vector-memory calls (`memory:*`) and file reads with the agent that
    performed them. This is intentionally best-effort: if Postgres/psycopg2 is
    unavailable, the dashboard should keep working.
    """
    def _iso_with_timezone(ts) -> str:
        if not hasattr(ts, "isoformat"):
            return str(ts or "")
        try:
            import datetime as _dt
            if ts.tzinfo is None:
                local_tz = _dt.datetime.now().astimezone().tzinfo
                ts = ts.replace(tzinfo=local_tz)
            return ts.isoformat()
        except Exception:
            return ts.isoformat()

    db_url = _read_hermes_env_value("HERMES_LOG_DB_URL")
    if not db_url:
        return {"rows": [], "counts": {}, "error": "HERMES_LOG_DB_URL not configured"}
    limit = max(1, min(int(limit or 80), 200))
    try:
        import psycopg2
        import psycopg2.extras
    except Exception as exc:
        return {"rows": [], "counts": {}, "error": f"psycopg2 unavailable: {exc}"}

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
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
        rows = []
        counts: dict[str, int] = {}
        for r in cur.fetchall():
            tool = r.get("tool") or ""
            counts[tool] = counts.get(tool, 0) + 1
            ts = r.get("ts")
            extra = r.get("extra") or {}
            if tool in ("x:search", "tavily:search", "tavily:extract"):
                provider = "x" if tool == "x:search" else "tavily"
                query = extra.get("query") or extra.get("url") or r.get("detail") or ""
                _credit_external_tool(
                    provider,
                    r.get("agent") or "unknown",
                    tool.replace(":", "_"),
                    str(query),
                    session_id=r.get("session_id") or "",
                    duration_ms=r.get("duration_ms"),
                    detail=r.get("detail") or "",
                    extra={"tool_activity_id": r.get("id")},
                )
            rows.append({
                "id": r.get("id"),
                "ts": _iso_with_timezone(ts),
                "agent": r.get("agent") or "unknown",
                "tool": tool,
                "detail": r.get("detail") or "",
                "session_id": r.get("session_id") or "",
                "duration_ms": r.get("duration_ms"),
                "extra": extra,
            })
        conn.close()
        return {"rows": rows, "counts": counts}
    except Exception as exc:
        return {"rows": [], "counts": {}, "error": str(exc)}

# Kanban snapshot from the most recent poll. Used to compute deltas so we only
# broadcast cards whose status actually changed.
kanban_snapshot: dict = {}  # task_id -> last-seen status string
kanban_snapshot_lock = threading.Lock()

# Ring buffer of the last N broadcast events. Replayed to new SSE clients so the
# dashboard reflects current state instead of a blank slate. Heartbeats and
# log_line entries are excluded — they're high-frequency and not useful as
# backlog.
RECENT_EVENT_LIMIT = 200
recent_events: collections.deque = collections.deque(maxlen=RECENT_EVENT_LIMIT)
recent_events_lock = threading.Lock()

# Poll interval (seconds) for the Watcher loop. Lower = more responsive feed,
# higher = less CPU. 100 ms is a good default; override with HERMES_WEBUI_POLL.
POLL_INTERVAL = float(os.environ.get("HERMES_WEBUI_POLL", "0.1"))

# File registry: list of dicts with keys agent/filename/path/size/mtime/ext
file_registry: list[dict] = []
file_registry_lock = threading.Lock()
registered_paths: dict[str, float] = {}  # realpath -> last registered mtime
FILE_REGISTRY_MAX_AGE_SEC = float(os.environ.get("HERMES_FILES_MAX_AGE_DAYS", "14")) * 86400
FILE_REGISTRY_MAX_ENTRIES = int(os.environ.get("HERMES_FILES_MAX_ENTRIES", "80"))
FILE_POLL_CACHE_IMAGES = os.environ.get("HERMES_FILES_POLL_CACHE", "1").strip().lower() not in (
    "0", "false", "no",
)


# ── Slack trace mirror (Option B) ──────────────────────────────────────────────
#
# Posts system-side events (dispatch delays, file writes, kanban transitions,
# errors) to a dedicated trace channel. Agents post their own summary lines via
# the send_message tool per their SOUL/AGENTS rules; we only mirror events the
# agents can't see from their own context. This avoids duplicate posts.
#
# Reads SLACK_BOT_TOKEN + SLACK_TRACE_CHANNEL from ~/.hermes/.env at startup.
# If either is missing, mirroring is silently disabled.

def _read_env_file(path: str) -> dict[str, str]:
    """Tiny .env parser — KEY=VALUE per line, ignores comments and blanks."""
    out: dict[str, str] = {}
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k:
                    out[k] = v
    except OSError:
        pass
    return out


_sage_env = _read_env_file(os.path.join(HERMES_DIR, ".env"))
SLACK_TRACE_TOKEN   = (os.environ.get("SLACK_TRACE_TOKEN")
                       or _sage_env.get("SLACK_BOT_TOKEN", ""))
SLACK_TRACE_CHANNEL = (os.environ.get("SLACK_TRACE_CHANNEL")
                       or _sage_env.get("SLACK_TRACE_CHANNEL", ""))
TRACE_ENABLED = bool(SLACK_TRACE_TOKEN and SLACK_TRACE_CHANNEL)
TRACE_MODE = os.environ.get("HERMES_TRACE_MODE", "milestones").strip().lower()

# Queue + worker for Slack posts. Bounded so a stuck Slack API can't blow
# memory. Rate-limited to roughly 1 msg/sec to stay under Slack's tier-1 cap.
trace_queue: queue.Queue = queue.Queue(maxsize=500)
_trace_milestone_seen: set[tuple[str, str]] = set()
_trace_incident_seen: dict[str, float] = {}
_memory_search_pending: dict[str, str] = {}  # tool_call_id → query label
_INCIDENT_DEDUPE_TTL_S = 300
def _agent_emoji(agent_id: str) -> str:
    meta = _KNOWN_AGENT_META.get(agent_id)
    return meta["emoji"] if meta else "•"
# local_path (realpath) → presigned S3 URL, populated from s3_upload tool results
_s3_url_by_local_path: dict[str, str] = {}


def _post_to_slack(text: str) -> None:
    """POST a single chat.postMessage to the trace channel. Stdlib only."""
    if not TRACE_ENABLED:
        return
    import urllib.request

    body = json.dumps({
        "channel": SLACK_TRACE_CHANNEL,
        "text": text[:3500],  # Slack hard-caps at 4000 chars; leave headroom
    }).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=body,
        headers={
            "Authorization": f"Bearer {SLACK_TRACE_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            if not payload.get("ok"):
                print(f"[trace] slack post failed: {payload.get('error')}", file=sys.stderr)
    except Exception as exc:
        print(f"[trace] slack post error: {exc}", file=sys.stderr)


def _trace_worker() -> None:
    """Pull lines off trace_queue, throttle to 1/sec, POST."""
    while True:
        text = trace_queue.get()
        if text is None:
            return
        _post_to_slack(text)
        # Slack tier-1 method limit is ~1 msg/sec/channel. Stay safely under.
        time.sleep(1.0)


def trace(text: str) -> None:
    """Enqueue a one-line trace message. No-op if mirroring is disabled."""
    if not TRACE_ENABLED:
        return
    try:
        trace_queue.put_nowait(text)
    except queue.Full:
        pass  # drop on overflow rather than blocking the watcher


def _trace_verbose() -> bool:
    return TRACE_MODE == "verbose"


def _trace_dedupe_milestone(key: tuple[str, str]) -> bool:
    """Return True if this milestone was already posted (and record it)."""
    if key in _trace_milestone_seen:
        return True
    _trace_milestone_seen.add(key)
    if len(_trace_milestone_seen) > 2000:
        _trace_milestone_seen.clear()
    return False


def _short_session(session_id: str | None) -> str:
    if not session_id:
        return ""
    parts = session_id.split("_")
    return parts[-1][:8] if parts else session_id[:8]


def trace_incident(
    agent_id: str,
    kind: str,
    detail: str,
    *,
    severity: str = "error",
    session_id: str | None = None,
) -> None:
    """Always-on incident posts — not suppressed in milestone mode."""
    if not TRACE_ENABLED:
        return
    prefix = {"error": "🚨", "warn": "⚠️", "blocked": "⛔"}.get(severity, "🚨")
    sess = f" · session `{_short_session(session_id)}`" if session_id else ""
    dedupe_key = f"{agent_id}:{kind}:{detail[:80]}"
    now = time.time()
    if now - _trace_incident_seen.get(dedupe_key, 0) < _INCIDENT_DEDUPE_TTL_S:
        return
    _trace_incident_seen[dedupe_key] = now
    body = f"{prefix} `{agent_id}` · {kind} · {detail[:200]}{sess}"
    trace(body)


def _session_from_log_line(line: str) -> str:
    m = re.search(r"\[(?P<session>[^\]]+)\]", line)
    return m.group("session") if m else ""


def _kanban_completion_detail(task_id: str) -> dict | None:
    """Latest run summary + metadata for a kanban task (read-only)."""
    conn = _kanban_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT summary, metadata, error, status FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        meta_raw = row[1]
        try:
            meta = json.loads(meta_raw) if meta_raw else {}
        except json.JSONDecodeError:
            meta = {"_raw": meta_raw}
        return {
            "summary": row[0] or "",
            "metadata": meta,
            "error": row[2] or "",
            "status": row[3] or "",
        }
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _cache_s3_upload(local_path: str, url: str) -> None:
    if not local_path or not url:
        return
    try:
        _s3_url_by_local_path[os.path.realpath(local_path)] = url
    except OSError:
        _s3_url_by_local_path[local_path] = url


def _lookup_s3_url(local_path: str) -> str | None:
    if not local_path:
        return None
    try:
        hit = _s3_url_by_local_path.get(os.path.realpath(local_path))
    except OSError:
        hit = None
    return hit or _s3_url_by_local_path.get(local_path)


def _ensure_s3_url(local_path: str) -> str | None:
    """Upload to S3 if needed and return presigned URL (Monitor-side, best-effort)."""
    cached = _lookup_s3_url(local_path)
    if cached:
        return cached
    try:
        if not os.path.isfile(local_path):
            return None
        if HERMES_DIR not in sys.path:
            sys.path.insert(0, HERMES_DIR)
        from s3_store import is_available, upload_workspace_file
        if not is_available():
            return None
        url = upload_workspace_file(local_path)
        _cache_s3_upload(local_path, url)
        return url
    except Exception:
        return None


def _resolve_output_link(meta: dict) -> str | None:
    """Prefer metadata s3_url; fall back to s3_upload cache or direct upload."""
    for key in ("s3_url", "url"):
        val = meta.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    path_keys = ("output_path", "image_path", "local_path", "file_output")
    for key in path_keys:
        lp = meta.get(key)
        if isinstance(lp, str) and lp.strip():
            cached = _lookup_s3_url(lp.strip())
            if cached:
                return cached
    artifacts = meta.get("artifacts")
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, str) and item.strip():
                cached = _lookup_s3_url(item.strip())
                if cached:
                    return cached
    for key in path_keys:
        lp = meta.get(key)
        if isinstance(lp, str) and lp.strip():
            uploaded = _ensure_s3_url(lp.strip())
            if uploaded:
                return uploaded
    if isinstance(artifacts, list):
        for item in artifacts:
            if isinstance(item, str) and item.strip():
                uploaded = _ensure_s3_url(item.strip())
                if uploaded:
                    return uploaded
    return None


def _sanitize_trace_text(text: str) -> str:
    """Strip tool-call leakage and replace local paths with S3 URLs or basenames."""
    if not text:
        return ""
    cleaned = text.strip()
    for marker in ("<parameter", "<tool_call", "<function"):
        idx = cleaned.find(marker)
        if idx > 0:
            cleaned = cleaned[:idx].rstrip('", \n')
    # Replace absolute paths — S3 URL if cached, else filename only
    def _path_sub(m: re.Match) -> str:
        path = m.group(0)
        cached = _lookup_s3_url(path)
        if cached:
            return cached
        return os.path.basename(path.rstrip("/"))

    cleaned = re.sub(r"/Users[^\s\"'`<>]+", _path_sub, cleaned)
    cleaned = re.sub(r"~/.hermes[^\s\"'`<>]+", _path_sub, cleaned)
    return cleaned.strip()


def _format_done_milestone(card: dict, detail: dict | None) -> str:
    """Rich worker-done line (2–4 lines max) from card + task_runs."""
    tid = card.get("id", "?")
    assignee = card.get("assignee") or "?"
    title = (card.get("title") or "")[:80]
    em = _agent_emoji(assignee)
    head = f"✅ {em} `{assignee}` · `{tid}` done · _{title}_"
    lines = [head]
    if detail:
        raw_summary = (detail.get("summary") or "").strip()
        meta = dict(detail.get("metadata") or {})
        if not meta.get("output_path"):
            pm = re.search(r'"output_path"\s*:\s*"([^"]+)"', raw_summary)
            if pm:
                meta["output_path"] = pm.group(1)
        if not meta.get("file_output") and meta.get("output_path"):
            meta["file_output"] = meta["output_path"]
        if not meta.get("artifacts") and meta.get("file_output"):
            meta["artifacts"] = [meta["file_output"]]
        if not meta.get("s3_url"):
            sm = re.search(r'"(?:s3_url|url)"\s*:\s*"(https?://[^"]+)"', raw_summary)
            if sm:
                meta["s3_url"] = sm.group(1)
        summary = _sanitize_trace_text(raw_summary)
        if summary:
            lines.append(summary[:400])
        chips: list[str] = []
        layers = meta.get("layers_used")
        if layers:
            if isinstance(layers, list):
                chips.append("layers: " + ", ".join(str(x) for x in layers))
            else:
                chips.append(f"layers: {layers}")
        kf = meta.get("key_findings")
        if isinstance(kf, list):
            for bullet in kf[:2]:
                lines.append(f"• {_sanitize_trace_text(str(bullet))[:120]}")
        gaps = meta.get("gaps_flagged")
        if gaps:
            gtxt = gaps if isinstance(gaps, str) else f"{len(gaps)}"
            chips.append(f"gaps: {gtxt}")
        out_link = _resolve_output_link(meta)
        out_path = meta.get("output_path") or meta.get("image_path")
        s3_url = meta.get("s3_url") or meta.get("url")
        if out_link:
            chips.append(f"output: {out_link}")
        elif out_path:
            chips.append(f"output: `{os.path.basename(str(out_path))}`")
        qr = meta.get("queries_run")
        if isinstance(qr, list) and qr:
            chips.append(f"queries: {len(qr)}")
        if chips:
            lines.append(" · ".join(chips))
    elif card.get("elapsed_s") is not None:
        lines[0] += f" · `{card['elapsed_s']}s`"
    return "\n".join(lines[:4])


def _trace_done_incidents(card: dict, detail: dict | None) -> None:
    if not detail:
        return
    meta = detail.get("metadata") or {}
    assignee = card.get("assignee") or "?"
    tid = card.get("id", "?")
    if meta.get("degraded"):
        trace_incident(assignee, "done degraded", f"degraded completion · `{tid}`", severity="warn")
    needs = meta.get("needs_signal")
    if needs:
        trace_incident(
            assignee, "gaps", f"needs_signal: {str(needs)[:100]} · `{tid}`", severity="warn",
        )
    gaps = meta.get("gaps_flagged")
    if gaps:
        gdesc = gaps if isinstance(gaps, str) else f"{len(gaps)} items"
        trace_incident(
            assignee, "gaps", f"gaps_flagged: {gdesc} · `{tid}`", severity="warn",
        )


def _trace_milestone_inbound(agent_id: str, msg: str, platform: str, chat: str) -> None:
    if _trace_verbose():
        return
    em = _agent_emoji(agent_id)
    snippet = (msg or "").replace("\n", " ")[:120]
    chan = chat if chat else platform or "?"
    trace(f"{em} `{agent_id}` · user in `{chan}` · \"{snippet}\"")


def _trace_milestone_delivered(
    agent_id: str, elapsed_s: float, api_calls: int, response_chars: int,
) -> None:
    if _trace_verbose():
        return
    em = _agent_emoji(agent_id)
    trace(
        f"{em} `{agent_id}` · delivered to front · {response_chars:,} chars · "
        f"{api_calls} API calls · {elapsed_s:.1f}s"
    )


_MEMORY_SEARCH_CMD = re.compile(
    r"memory\.py\s+search\s+[\"'](.+?)[\"']",
    re.IGNORECASE | re.DOTALL,
)
_MEMORY_TYPE_FLAG = re.compile(r"--type\s+(\w+)", re.IGNORECASE)
_MEMORY_HITS_LINE = re.compile(r"(\d+)\s+memories returned", re.IGNORECASE)
_MEMORY_TOP_MATCH = re.compile(r"\[\d+\]\s+(\d+)%\s+match", re.IGNORECASE)


def _memory_search_label(command: str) -> str:
    """Short human label for a memory.py search terminal command."""
    cmd = (command or "").replace("\n", " ").strip()
    m = _MEMORY_SEARCH_CMD.search(cmd)
    query = m.group(1).strip() if m else "?"
    if len(query) > 70:
        query = query[:69] + "…"
    type_m = _MEMORY_TYPE_FLAG.search(cmd)
    type_tag = f" · {type_m.group(1)}" if type_m else ""
    return f"{query}{type_tag}"


def _parse_memory_search_output(content: str) -> tuple[int | None, int | None]:
    """Return (hit_count, top_match_pct) from memory.py stdout JSON wrapper."""
    output = content
    try:
        parsed = json.loads(content) if content.strip().startswith("{") else None
        if isinstance(parsed, dict):
            output = str(parsed.get("output") or parsed.get("result") or content)
    except (json.JSONDecodeError, TypeError):
        pass
    hits_m = _MEMORY_HITS_LINE.search(output)
    hits = int(hits_m.group(1)) if hits_m else None
    top_m = _MEMORY_TOP_MATCH.search(output)
    top_pct = int(top_m.group(1)) if top_m else None
    return hits, top_pct


def _trace_milestone_memory_search(
    agent_id: str,
    query_label: str,
    hits: int | None,
    top_pct: int | None,
    session_id: str | None,
) -> None:
    """Post a Slack trace milestone when Sage completes a vector memory search."""
    if _trace_verbose():
        return
    dedupe_key = (session_id or "?", f"mem:{query_label[:80]}")
    if _trace_dedupe_milestone(dedupe_key):
        return
    em = _agent_emoji(agent_id)
    hit_s = f"{hits} hit(s)" if hits is not None else "done"
    top_s = f" · top {top_pct}%" if top_pct is not None else ""
    trace(
        f"{em} `{agent_id}` · preflight · memory.search · \"{query_label}\" · "
        f"{hit_s}{top_s}"
    )


def _track_memory_preflight_from_event(
    agent_id: str,
    ev: dict,
    session_short: str | None,
) -> None:
    """Detect memory.py terminal searches and emit trace milestones on completion."""
    role = ev.get("role", "")
    if role == "assistant":
        for tc in ev.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            if fn.get("name") != "terminal":
                continue
            raw_args = fn.get("arguments") or ""
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except (json.JSONDecodeError, TypeError):
                args = {}
            cmd = str(args.get("command") or "")
            if "memory.py" not in cmd or " search " not in cmd:
                continue
            call_id = str(tc.get("id") or tc.get("tool_call_id") or "")
            if call_id:
                _memory_search_pending[call_id] = _memory_search_label(cmd)
        return
    if role != "tool":
        return
    tool_name = ev.get("tool_name") or ev.get("name", "")
    if tool_name != "terminal":
        return
    call_id = str(ev.get("tool_call_id") or ev.get("id") or "")
    query_label = _memory_search_pending.pop(call_id, None)
    if not query_label:
        return
    content = str(ev.get("content") or "")
    is_error = False
    try:
        parsed = json.loads(content) if content.strip().startswith("{") else None
        if isinstance(parsed, dict):
            if parsed.get("error") or parsed.get("isError") or parsed.get("status") == "error":
                is_error = True
            if parsed.get("exit_code") not in (None, 0):
                is_error = True
    except (json.JSONDecodeError, TypeError):
        pass
    if is_error:
        trace_incident(
            agent_id,
            "memory search failed",
            f"\"{query_label[:80]}\"",
            session_id=session_short,
            severity="warn",
        )
        return
    hits, top_pct = _parse_memory_search_output(content)
    _trace_milestone_memory_search(agent_id, query_label, hits, top_pct, session_short)


# ── File registry ──────────────────────────────────────────────────────────────

def _agent_id_for_path(path: str) -> str | None:
    """Map an on-disk path to a monitor agent id when under workspace or cache."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    hermes = os.path.realpath(HERMES_DIR)
    if not real.startswith(hermes + os.sep):
        return None
    for aid in AGENT_IDS:
        ws = os.path.realpath(os.path.join(WORKSPACE_DIR, aid))
        if real.startswith(ws + os.sep):
            return aid
    if real.startswith(os.path.realpath(MEDIA_DIR) + os.sep):
        return "imagine"
    return None


def _file_age_ok(mtime: float) -> bool:
    return (time.time() - mtime) <= FILE_REGISTRY_MAX_AGE_SEC


def _prune_file_registry_locked() -> int:
    """Drop stale entries; keep newest `FILE_REGISTRY_MAX_ENTRIES`. Returns removed count."""
    before = len(file_registry)
    now = time.time()
    kept = [e for e in file_registry if _file_age_ok(e.get("mtime", 0))]
    kept.sort(key=lambda e: e.get("mtime", 0))
    if len(kept) > FILE_REGISTRY_MAX_ENTRIES:
        kept = kept[-FILE_REGISTRY_MAX_ENTRIES:]
    file_registry[:] = kept
    registered_paths.clear()
    registered_paths.update({e["path"]: e["mtime"] for e in file_registry})
    return before - len(file_registry)


def _try_register_path(path: str, *, broadcast_event: bool = True) -> bool:
    """Register a delivery file discovered outside directory polling."""
    path = (path or "").strip()
    if not path:
        return False
    aid = _agent_id_for_path(path)
    if not aid:
        return False
    return register_file(aid, path, broadcast_event=broadcast_event)


def _find_card_for_file(path: str) -> str | None:
    """Look up the kanban card whose run metadata mentions this file path.
    Best-effort; returns the most recent matching task_id, or None.
    """
    conn = _kanban_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        # Use LIKE because metadata is stored as a JSON string blob.
        cur.execute(
            "SELECT task_id FROM task_runs "
            "WHERE metadata LIKE ? "
            "ORDER BY id DESC LIMIT 1",
            (f"%{path}%",),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def register_file(agent_id: str, path: str, broadcast_event: bool = True) -> bool:
    """Add or refresh a file in the registry; returns True if newly added or updated."""
    try:
        real_path = os.path.realpath(path)
    except OSError:
        return False
    try:
        stat = os.stat(real_path)
        size = stat.st_size
        mtime = stat.st_mtime
    except OSError:
        return False
    if not _file_age_ok(mtime):
        return False

    prev_mtime = registered_paths.get(real_path)
    is_new = prev_mtime is None
    is_updated = prev_mtime is not None and mtime > prev_mtime + 0.5
    if not is_new and not is_updated:
        return False

    # If stub, report actual image size so the UI doesn't show "72B"
    actual = _resolve_file_path(real_path)
    if actual != real_path:
        try:
            size = os.stat(actual).st_size
        except OSError:
            pass
    ext = os.path.splitext(real_path)[1].lower()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
    card_id = _find_card_for_file(real_path)
    entry = {
        "agent": agent_id,
        "filename": os.path.basename(real_path),
        "path": real_path,
        "size": size,
        "mtime": mtime,
        "ts": ts,
        "ext": ext,
        "card_id": card_id,
    }
    with file_registry_lock:
        existing = next((e for e in file_registry if e["path"] == real_path), None)
        if existing:
            existing.update(entry)
        else:
            file_registry.append(entry)
        registered_paths[real_path] = mtime
        _prune_file_registry_locked()
    if broadcast_event:
        broadcast({"type": "file_event", **entry})
        if _trace_verbose():
            size_kb = max(1, size // 1024)
            em = _agent_emoji(agent_id)
            trace(f"{em} `{agent_id}` · wrote `{entry['filename']}` ({size_kb} KB)")
    return True


MEDIA_DIR = os.path.join(HERMES_DIR, "cache", "images")


def _is_allowed_file_path(path: str) -> bool:
    """Security check: only serve files within agent workspaces, output dirs, or media."""
    real = os.path.realpath(path)
    if real.startswith(os.path.realpath(MEDIA_DIR) + os.sep):
        return True
    for aid in AGENT_IDS:
        ws = os.path.realpath(os.path.join(WORKSPACE_DIR, aid))
        if real.startswith(ws + os.sep):
            return True
    return False


def _resolve_file_path(path: str) -> str:
    """If path is a binary-content placeholder stub, return the actual file path."""
    try:
        with open(path, "rb") as f:
            header = f.read(22)
        if header.startswith(b"[Binary content from /"):
            with open(path) as f:
                stub = f.read().strip()
            if stub.startswith("[Binary content from ") and stub.endswith("]"):
                actual = stub[len("[Binary content from "):-1]
                if os.path.isfile(actual):
                    return actual
    except OSError:
        pass
    return path


# ── Helpers ────────────────────────────────────────────────────────────────────

def broadcast(event: dict):
    payload = f"data: {json.dumps(event)}\n\n"
    # Keep a tail of meaningful events so new SSE clients can replay context.
    if event.get("type") in ("agent_event", "agent_status", "file_event"):
        with recent_events_lock:
            recent_events.append(event)
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


# ── Kanban (durable inter-agent channel) ──────────────────────────────────────
#
# In Hermes, agents talk to each other through the Kanban board: Sage creates
# a card, the dispatcher spawns the assignee, the worker writes back a
# completion summary + metadata. The kanban.db file is the source of truth
# and the equivalent of OpenClaw's "back-channel" Slack relay.
#
# We open the DB read-only so the gateway's writes never race with our reads.


def _kanban_conn():
    try:
        # uri=True + mode=ro avoids any chance of write contention with the
        # gateway's dispatcher.
        return sqlite3.connect(
            f"file:{KANBAN_DB}?mode=ro",
            uri=True, timeout=2.0, check_same_thread=False,
        )
    except sqlite3.Error:
        return None


def _kanban_conn_rw():
    try:
        return sqlite3.connect(KANBAN_DB, timeout=2.0, check_same_thread=False)
    except sqlite3.Error:
        return None


def _kanban_task_status(task_id: str) -> str | None:
    conn = _kanban_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _hermes_kanban_run(*args: str) -> tuple[bool, str]:
    """Run a hermes kanban subcommand. Returns (success, combined output).

    Hermes often exits 0 even when it prints 'cannot ...' — treat that as failure.
    """
    try:
        cp = subprocess.run(
            ["hermes", "kanban", *args],
            capture_output=True, text=True, timeout=10,
            env=os.environ,
        )
    except Exception as exc:
        return False, str(exc)
    out = ((cp.stdout or "") + (cp.stderr or "")).strip()
    if cp.returncode != 0:
        return False, out
    lower = out.lower()
    if any(p in lower for p in ("cannot ", "no such task", "unknown id", "not found")):
        return False, out
    return True, out


def _kanban_archive_direct(task_id: str, prev_status: str | None = None) -> dict:
    """Archive a task directly in kanban.db when the hermes CLI cannot."""
    conn = _kanban_conn_rw()
    if not conn:
        return {"ok": False, "error": "kanban.db unavailable"}
    try:
        cur = conn.cursor()
        if prev_status is None:
            cur.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": f"task {task_id} not found"}
            prev_status = row[0]
        if prev_status == "archived":
            return {"ok": True, "archived": task_id, "method": "direct", "noop": True}
        now = int(time.time())
        cur.execute(
            "UPDATE tasks SET status='archived', claim_lock=NULL, claim_expires=NULL, "
            "worker_pid=NULL, current_run_id=NULL "
            "WHERE id = ? AND status != 'archived'",
            (task_id,),
        )
        if cur.rowcount == 0:
            return {"ok": False, "error": f"task {task_id} not found or already archived"}
        cur.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "archived", json.dumps({"source": "hermes-monitor", "prev_status": prev_status}), now),
        )
        conn.commit()
        return {"ok": True, "archived": task_id, "method": "direct"}
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def _kanban_reclaim_direct(task_id: str) -> bool:
    """Release an active worker claim directly in kanban.db."""
    conn = _kanban_conn_rw()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tasks SET claim_lock=NULL, claim_expires=NULL, worker_pid=NULL, "
            "current_run_id=NULL WHERE id = ? AND status = 'running'",
            (task_id,),
        )
        changed = cur.rowcount > 0
        if changed:
            cur.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "reclaimed", json.dumps({"source": "hermes-monitor"}), int(time.time())),
            )
        conn.commit()
        return changed
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _kanban_refresh_after_change(task_id: str) -> None:
    try:
        with kanban_snapshot_lock:
            kanban_snapshot.pop(task_id, None)
        _resolve_prefix(f"task_blocked:{task_id}")
        _resolve_prefix(f"stalled_task:{task_id}")
        board = kanban_board_summary()
        broadcast({"type": "kanban_board", **board})
    except Exception:
        pass


def kanban_board_summary(limit_done: int = 30) -> dict:
    """Return the active board: every ready/running/blocked card plus the most
    recent `limit_done` finished ones. Shape is small enough to ship over SSE
    on each change.
    """
    conn = _kanban_conn()
    if not conn:
        return {"available": False, "tasks": [], "counts": {}}
    try:
        cur = conn.cursor()
        # Active (non-terminal) tasks: ready, todo, triage, running, blocked, scheduled
        cur.execute(
            "SELECT id, title, assignee, status, created_at, started_at, completed_at "
            "FROM tasks WHERE status NOT IN ('done', 'archived') "
            "ORDER BY created_at DESC"
        )
        active = [_row_to_card(r) for r in cur.fetchall()]
        # Recent done — terminal but worth showing for trace context
        cur.execute(
            "SELECT id, title, assignee, status, created_at, started_at, completed_at "
            "FROM tasks WHERE status = 'done' "
            "ORDER BY completed_at DESC LIMIT ?",
            (limit_done,),
        )
        done = [_row_to_card(r) for r in cur.fetchall()]
        # Counts across full table for the status pill
        cur.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        counts = {row[0]: row[1] for row in cur.fetchall()}
        return {
            "available": True,
            "tasks": active + done,
            "counts": counts,
        }
    except sqlite3.Error as e:
        return {"available": False, "error": str(e), "tasks": [], "counts": {}}
    finally:
        conn.close()


def _row_to_card(row) -> dict:
    tid, title, assignee, status, created_at, started_at, completed_at = row
    elapsed = None
    if started_at:
        end = completed_at or int(time.time())
        elapsed = max(0, end - started_at)
    return {
        "id": tid,
        "title": title,
        "assignee": assignee or "",
        "status": status,
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "elapsed_s": elapsed,
    }


# Per-agent launchd control. Pure-worker profiles (ink, etc.) intentionally
# have no plist — they're spawned on demand by Sage's dispatcher.
AGENT_PLISTS = {
    "sage": os.path.expanduser("~/Library/LaunchAgents/ai.hermes.gateway.plist"),
    "imagine": os.path.expanduser("~/Library/LaunchAgents/ai.hermes.gateway-imagine.plist"),
}
AGENT_LAUNCHD_LABELS = {
    "sage": "ai.hermes.gateway",
    "imagine": "ai.hermes.gateway-imagine",
}

# When HERMES_DOCKER_CONTAINER is set the monitor routes all agent lifecycle
# actions to `docker start/stop/restart <container>` instead of launchctl.
# Set this in start-lab.sh when the gateway runs inside Docker.
DOCKER_CONTAINER = os.environ.get("HERMES_DOCKER_CONTAINER", "").strip()


def _get_worker_pids(agent_id: str) -> list[int]:
    """Return PIDs of in-progress kanban workers for agent_id (Docker mode only)."""
    db_path = os.path.join(HERMES_DIR, "kanban.db")
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        rows = conn.execute(
            "SELECT worker_pid FROM tasks "
            "WHERE status='in_progress' AND assignee=? AND worker_pid IS NOT NULL",
            (agent_id,)
        ).fetchall()
        conn.close()
        return [int(r[0]) for r in rows if r[0]]
    except Exception:
        return []


def _agent_running(agent_id: str) -> bool:
    """Return True if the agent's gateway is currently running."""
    if DOCKER_CONTAINER:
        if agent_id == HERMES_ORCHESTRATOR:
            try:
                cp = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Running}}", DOCKER_CONTAINER],
                    capture_output=True, text=True, timeout=5,
                )
                return cp.returncode == 0 and cp.stdout.strip() == "true"
            except Exception:
                return False
        else:
            return len(_get_worker_pids(agent_id)) > 0
    label = AGENT_LAUNCHD_LABELS.get(agent_id)
    if not label:
        return False
    try:
        cp = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=5,
        )
        return cp.returncode == 0
    except Exception:
        return False


def agent_lifecycle(agent_id: str, action: str) -> dict:
    """start / stop / restart an agent's gateway.

    In Docker mode (HERMES_DOCKER_CONTAINER set) all actions route to
    `docker start/stop/restart`. Otherwise uses launchd plists.
    Pure workers (no gateway) return 400. Unknown agent → 404.
    """
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": f"unknown action: {action}"}

    def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str]:
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return cp.returncode, (cp.stderr or cp.stdout).strip()
        except Exception as exc:
            return 1, str(exc)

    # ── Docker mode ───────────────────────────────────────────────────────────
    if DOCKER_CONTAINER:
        if agent_id == HERMES_ORCHESTRATOR:
            rc, msg = _run(["docker", action, DOCKER_CONTAINER])
            if rc != 0:
                return {"ok": False, "message": msg}
            try:
                broadcast({"type": "agent_status", "agent": agent_id, "active": False,
                           "last_seen": None, "session": None})
            except Exception:
                pass
            return {"ok": True, "action": action, "container": DOCKER_CONTAINER}
        else:
            if action == "start":
                return {"ok": False, "error": (
                    f"{agent_id} workers are started by the kanban dispatcher — "
                    "send a task via Slack to dispatch one"
                )}
            pids = _get_worker_pids(agent_id)
            if not pids:
                return {"ok": True, "action": action,
                        "note": f"no active {agent_id} workers found"}
            results = []
            for pid in pids:
                rc, _ = _run(["docker", "exec", DOCKER_CONTAINER, "kill", "-TERM", str(pid)])
                results.append({"pid": pid, "ok": rc == 0})
            return {
                "ok": True, "action": action, "container": DOCKER_CONTAINER,
                "workers_signaled": results,
                "note": "dispatcher will respawn pending tasks automatically" if action == "restart" else "",
            }

    # ── launchd mode ─────────────────────────────────────────────────────────
    plist = AGENT_PLISTS.get(agent_id)
    if plist is None:
        if agent_id in AGENT_IDS:
            return {
                "ok": False,
                "error": f"{agent_id} is a pure kanban worker — no gateway to control",
                "kind": "no_gateway",
            }
        return {"ok": False, "error": f"unknown agent: {agent_id}"}
    if not os.path.isfile(plist):
        return {"ok": False, "error": f"plist not found at {plist}"}

    if action == "start":
        if _agent_running(agent_id):
            return {"ok": True, "noop": True, "message": "already running"}
        rc, msg = _run(["launchctl", "load", plist])
        return {"ok": rc == 0, "message": msg} if rc != 0 else {"ok": True, "action": "started"}

    if action == "stop":
        if not _agent_running(agent_id):
            return {"ok": True, "noop": True, "message": "already stopped"}
        rc, msg = _run(["launchctl", "unload", plist])
        return {"ok": rc == 0, "message": msg} if rc != 0 else {"ok": True, "action": "stopped"}

    # restart
    _run(["launchctl", "unload", plist])
    time.sleep(0.5)
    rc, msg = _run(["launchctl", "load", plist])
    if rc != 0:
        return {"ok": False, "message": f"reload failed: {msg}"}
    try:
        broadcast({"type": "agent_status", "agent": agent_id, "active": False,
                   "last_seen": None, "session": None})
    except Exception:
        pass
    return {"ok": True, "action": "restarted"}


def kanban_cancel_one(task_id: str) -> dict:
    """Cancel a task regardless of its current state.

    For running tasks: reclaim (release the worker claim) first so the
    dispatcher doesn't re-spawn it, then archive.
    For all other states: archive directly.
    Falls back to direct kanban.db writes when the hermes CLI cannot handle
    the task (common for blocked cards in Docker lab boards).
    """
    if not task_id or not task_id.startswith("t_"):
        return {"ok": False, "error": "invalid task id"}

    status = _kanban_task_status(task_id)
    if status is None:
        return {"ok": False, "error": f"task {task_id} not found"}

    steps: list[str] = []

    if status == "running":
        ok, msg = _hermes_kanban_run("reclaim", task_id)
        if ok:
            steps.append("reclaimed")
        elif _kanban_reclaim_direct(task_id):
            steps.append("reclaimed_direct")
        elif msg:
            steps.append(f"reclaim_warn:{msg[:80]}")

    ok, msg = _hermes_kanban_run("archive", task_id)
    if ok and _kanban_task_status(task_id) in (None, "archived"):
        steps.append("archived")
        _kanban_refresh_after_change(task_id)
        return {"ok": True, "cancelled": task_id, "steps": steps}

    direct = _kanban_archive_direct(task_id, prev_status=status)
    if direct.get("ok"):
        steps.append("archived_direct")
        _kanban_refresh_after_change(task_id)
        return {"ok": True, "cancelled": task_id, "steps": steps}

    return {
        "ok": False,
        "error": direct.get("error") or msg or "archive failed",
        "steps": steps,
    }


def kanban_archive_one(task_id: str) -> dict:
    """Archive a single kanban task. Tries hermes CLI first, then direct DB write."""
    if not task_id or not task_id.startswith("t_"):
        return {"ok": False, "error": "invalid task id"}

    ok, msg = _hermes_kanban_run("archive", task_id)
    if ok and _kanban_task_status(task_id) in (None, "archived"):
        _kanban_refresh_after_change(task_id)
        return {"ok": True, "archived": [task_id], "method": "hermes"}

    direct = _kanban_archive_direct(task_id)
    if direct.get("ok"):
        _kanban_refresh_after_change(task_id)
        return {"ok": True, "archived": [task_id], "method": direct.get("method", "direct")}

    err = direct.get("error") or msg or "archive failed"
    return {"ok": False, "error": err}


def kanban_archive_done() -> dict:
    """Archive every task currently in `done` status. Returns counts."""
    try:
        conn = _kanban_conn()
    except Exception as exc:
        return {"ok": False, "error": f"kanban.db unavailable: {exc}"}
    try:
        cur = conn.execute(
            "SELECT id FROM tasks WHERE status='done' ORDER BY completed_at ASC"
        )
        ids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    archived: list[str] = []
    failed: list[dict] = []
    for tid in ids:
        result = kanban_archive_one(tid)
        if result.get("ok"):
            archived.append(tid)
        else:
            failed.append({"id": tid, "error": (result.get("error") or "unknown")[:120]})

    # Refresh the board snapshot + broadcast so the UI updates immediately.
    try:
        with kanban_snapshot_lock:
            for tid in archived:
                kanban_snapshot.pop(tid, None)
        board = kanban_board_summary()
        broadcast({"type": "kanban_board", **board})
    except Exception:
        pass
    return {"ok": True, "archived": archived, "failed": failed, "count": len(archived)}


def kanban_card_detail(task_id: str) -> dict:
    """Return one card with body, events, runs, comments, and links."""
    conn = _kanban_conn()
    if not conn:
        return {"error": "kanban.db not available"}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, body, assignee, status, priority, created_by, "
            "       created_at, started_at, completed_at, workspace_kind, "
            "       workspace_path, tenant, result, last_failure_error "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"error": "not found"}
        task = dict(zip(
            ["id", "title", "body", "assignee", "status", "priority",
             "created_by", "created_at", "started_at", "completed_at",
             "workspace_kind", "workspace_path", "tenant", "result",
             "last_failure_error"], row,
        ))

        cur.execute(
            "SELECT id, run_id, kind, payload, created_at "
            "FROM task_events WHERE task_id = ? ORDER BY created_at ASC, id ASC",
            (task_id,),
        )
        events = []
        for ev in cur.fetchall():
            payload_raw = ev[3]
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except json.JSONDecodeError:
                payload = {"_raw": payload_raw}
            events.append({
                "id": ev[0], "run_id": ev[1], "kind": ev[2],
                "payload": payload, "created_at": ev[4],
            })

        cur.execute(
            "SELECT id, profile, status, outcome, summary, metadata, error, "
            "       started_at, ended_at "
            "FROM task_runs WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        )
        runs = []
        for r in cur.fetchall():
            meta_raw = r[5]
            try:
                meta = json.loads(meta_raw) if meta_raw else {}
            except json.JSONDecodeError:
                meta = {"_raw": meta_raw}
            runs.append({
                "id": r[0], "profile": r[1], "status": r[2], "outcome": r[3],
                "summary": r[4], "metadata": meta, "error": r[6],
                "started_at": r[7], "ended_at": r[8],
            })

        # Comments (best-effort — table may or may not exist depending on schema version)
        comments = []
        try:
            cur.execute(
                "SELECT id, author, body, created_at "
                "FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            )
            for c in cur.fetchall():
                comments.append({
                    "id": c[0], "author": c[1], "body": c[2], "created_at": c[3],
                })
        except sqlite3.Error:
            pass

        # Parent/child links
        parents, children = [], []
        try:
            cur.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,))
            parents = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (task_id,))
            children = [r[0] for r in cur.fetchall()]
        except sqlite3.Error:
            pass

        # Worker log path (text) — read-tail it if it exists
        log_path = os.path.join(KANBAN_LOGS, f"{task_id}.log")
        log_tail = []
        if os.path.isfile(log_path):
            log_tail = read_last_lines(log_path, 80)

        return {
            "task": task,
            "events": events,
            "runs": runs,
            "comments": comments,
            "parents": parents,
            "children": children,
            "log_tail": log_tail,
        }
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        conn.close()


def backup_status() -> dict:
    configured = bool(BACKUP_REPO and BACKUP_SCRIPT and os.path.isfile(BACKUP_SCRIPT))
    base = {
        "configured": configured,
        "repo": BACKUP_REPO or None,
        "script": BACKUP_SCRIPT or None,
    }
    if not configured:
        return {
            **base,
            "ok": False,
            "error": (
                "Backup not configured. "
                "Set HERMES_BACKUP_REPO and HERMES_BACKUP_SCRIPT environment variables."
            ),
        }
    if not os.path.isdir(BACKUP_REPO):
        return {**base, "ok": False, "error": "Repo not found"}
    try:
        cp = subprocess.run(
            ["git", "log", "-1", "--format=%h|%s|%ci"],
            cwd=BACKUP_REPO, capture_output=True, text=True, timeout=5,
        )
        last_commit = None
        if cp.returncode == 0 and cp.stdout.strip():
            parts = cp.stdout.strip().split("|", 2)
            last_commit = {
                "hash": parts[0] if len(parts) > 0 else "",
                "message": parts[1] if len(parts) > 1 else "",
                "date": parts[2][:16] if len(parts) > 2 else "",
            }
        cp2 = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=BACKUP_REPO, capture_output=True, text=True, timeout=5,
        )
        dirty = bool(cp2.stdout.strip()) if cp2.returncode == 0 else None
        return {**base, "ok": True, "last_commit": last_commit, "dirty": dirty}
    except Exception as e:
        return {**base, "ok": False, "error": str(e)}


def run_backup() -> dict:
    configured = bool(BACKUP_REPO and BACKUP_SCRIPT and os.path.isfile(BACKUP_SCRIPT))
    if not configured:
        return {
            "ok": False,
            "configured": False,
            "error": (
                "Backup not configured. "
                "Set HERMES_BACKUP_REPO and HERMES_BACKUP_SCRIPT environment variables."
            ),
        }
    try:
        cp = subprocess.run(
            ["bash", BACKUP_SCRIPT],
            capture_output=True, text=True, timeout=180,
            cwd=BACKUP_REPO,
        )
        output = (cp.stdout + cp.stderr).strip()
        committed = "Committed:" in output
        commit_hash = None
        for line in output.splitlines():
            if line.startswith("COMMIT_HASH="):
                commit_hash = line.split("=", 1)[1].strip()
                break
        if not commit_hash and cp.returncode == 0:
            log = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=BACKUP_REPO, capture_output=True, text=True, timeout=5,
            )
            if log.returncode == 0:
                commit_hash = log.stdout.strip()
        return {
            "ok": cp.returncode == 0,
            "output": output,
            "configured": True,
            "repo": BACKUP_REPO,
            "script": BACKUP_SCRIPT,
            "committed": committed,
            "commit_hash": commit_hash,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "configured": True, "error": "Backup timed out (180s)"}
    except Exception as e:
        return {"ok": False, "configured": True, "error": str(e)}


def read_last_lines(path: str, n: int) -> list[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, n * 200)  # rough estimate
            f.seek(max(0, size - chunk))
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _state_db_path(agent_id: str) -> str:
    """Where Hermes stores sessions/messages for this profile."""
    return os.path.join(_agent_root(agent_id), "state.db")


def _state_row_to_event(row) -> dict:
    """Translate a state.db `messages` row into the dict shape that
    parse_hermes_event() expects. The session jsonl format and state.db
    schema differ slightly, so we normalize here.
    """
    ts_iso = ""
    try:
        ts = float(row["timestamp"] or 0)
        if ts > 0:
            import datetime
            ts_iso = datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except Exception:
        pass

    tool_calls = None
    raw_tc = row["tool_calls"]
    if raw_tc:
        try:
            tool_calls = json.loads(raw_tc) if isinstance(raw_tc, str) else raw_tc
        except Exception:
            tool_calls = None

    return {
        "role": row["role"],
        "content": row["content"] or "",
        "timestamp": ts_iso,
        "message_id": f"sqlite:{row['id']}",
        "tool_call_id": row["tool_call_id"] or "",
        "tool_calls": tool_calls,
        "tool_name": row["tool_name"],
        "name": row["tool_name"],
        "finish_reason": row["finish_reason"],
    }


def latest_session_file(agent_id: str) -> str | None:
    sessions_dir = os.path.join(_agent_root(agent_id), "sessions")
    if not os.path.isdir(sessions_dir):
        return None
    candidates = []
    for f in glob.glob(os.path.join(sessions_dir, "*.jsonl")):
        basename = os.path.basename(f)
        if "trajectory" in basename or ".bak" in basename or ".reset" in basename:
            continue
        candidates.append(f)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


# Patterns we recognise in agent.log and gateway.log. Each entry is
# (compiled_regex, builder_fn) where builder_fn(match, agent_id) returns the
# dict to broadcast — or None to skip.
import re as _re

_LIVE_LOG_PATTERNS: list = []

def _register_pattern(rx, builder):
    _LIVE_LOG_PATTERNS.append((_re.compile(rx), builder))


# Inbound user message on Slack (or any platform) — gateway.log.
# Also marks the start of a new per-prompt token-usage accumulator.
def _inbound_builder(m, aid):
    _start_prompt(aid, m.group("msg"))
    _trace_start(aid, m.group("msg"), m.group("platform"), m.group("user"), m.group("chat"))
    _trace_milestone_inbound(aid, m.group("msg"), m.group("platform"), m.group("chat"))
    return {
        "type": "agent_event",
        "agent": aid,
        "ts": _now_hms(),
        "kind": "user_message",
        "title": f"{m.group('msg')[:120]}",
        "full": f"[{m.group('user')}] via {m.group('platform')}\n{m.group('msg')}",
        "live": True,
    }

_register_pattern(
    r"gateway\.run:\s+inbound message:\s+platform=(?P<platform>\S+)\s+user=(?P<user>.+?)\s+chat=(?P<chat>\S+)\s+msg='(?P<msg>.+?)'",
    _inbound_builder,
)


def _response_ready_builder(m, aid):
    detail = f"{m.group('time')}s · {m.group('calls')} API calls · {m.group('chars')} chars"
    _trace_finish(aid, detail, elapsed_s=float(m.group("time")), api_calls=int(m.group("calls")), response_chars=int(m.group("chars")))
    _trace_milestone_delivered(
        aid, float(m.group("time")), int(m.group("calls")), int(m.group("chars")),
    )
    return {
        "type": "agent_event",
        "agent": aid,
        "ts": _now_hms(),
        "kind": "response",
        "title": f"Slack response ready · {detail}",
        "full": f"platform: {m.group('platform')}\nchat: {m.group('chat')}\nelapsed: {m.group('time')}s\napi_calls: {m.group('calls')}\nresponse_chars: {m.group('chars')}",
        "live": True,
    }


_register_pattern(
    r"gateway\.run:\s+response ready:\s+platform=(?P<platform>\S+)\s+chat=(?P<chat>\S+)\s+time=(?P<time>[\d.]+)s\s+api_calls=(?P<calls>\d+)\s+response=(?P<chars>\d+)\s+chars",
    _response_ready_builder,
)

def _tavily_search_builder(m, aid):
    query = m.group("query")
    _credit_external_tool("tavily", aid, "tavily_search", query, detail=f"Tavily search: {query}")
    _trace_add(aid, "tavily.search", query[:120])
    return {
        "type": "agent_event",
        "agent": aid,
        "ts": _now_hms(),
        "kind": "tool_call",
        "title": f"🔎 tavily.search · {query[:100]}",
        "detail": query,
        "full": f"## Tavily Search\n{query}",
        "live": True,
    }


def _tavily_extract_builder(m, aid):
    url = m.group("url")
    _credit_external_tool("tavily", aid, "tavily_extract", url, detail=f"Tavily extract: {url}")
    _trace_add(aid, "tavily.extract", url[:120])
    return {
        "type": "agent_event",
        "agent": aid,
        "ts": _now_hms(),
        "kind": "tool_call",
        "title": f"🌐 tavily.extract · {url[:100]}",
        "detail": url,
        "full": f"## Tavily Extract\n{url}",
        "live": True,
    }


# Tool started — many tool prologues land in agent.log as
#   "tools.<x>: <action>: '<query>'" or similar
_register_pattern(
    r"plugins\.web\.tavily\.provider:\s+Tavily search:\s+'(?P<query>.+?)'",
    _tavily_search_builder,
)

_register_pattern(
    r"plugins\.web\.tavily\.provider:\s+Tavily extract request to\s+(?P<url>\S+)",
    _tavily_extract_builder,
)

# Tool completion in agent.log: latency + chars
_register_pattern(
    r"agent\.tool_executor:\s+tool\s+(?P<name>\S+)\s+completed\s+\((?P<dur>[\d.]+)s,\s+(?P<chars>\d+)\s+chars\)",
    lambda m, aid: {
        "type": "agent_event",
        "agent": aid,
        "ts": _now_hms(),
        "kind": "tool_result",
        "title": f"✓ {m.group('name')} ({m.group('dur')}s, {int(m.group('chars')):,} chars)",
        "live": True,
    },
)

def _tool_returned_error_builder(m, aid):
    name = m.group("name")
    trace_incident(aid, "tool_error", name, session_id=_session_from_log_line(m.string))
    return {
        "type": "agent_event",
        "agent": aid,
        "ts": _now_hms(),
        "kind": "tool_error",
        "title": f"✗ {name} returned error",
        "live": True,
    }


_register_pattern(
    r"agent\.tool_executor:\s+tool\s+(?P<name>\S+)\s+returned error",
    _tool_returned_error_builder,
)

# Model API call summary — shown so the user can see "Sage is thinking" cycles.
# Also credits tokens to the per-prompt accumulator.
def _api_call_builder(m, aid):
    in_t = int(m.group("in_t"))
    out_t = int(m.group("out_t"))
    # Optional cache info — present when the line has "cache=X/Y" suffix
    cache_hits, cache_reads = 0, 0
    cmatch = _re.search(r"cache=(\d+)/(\d+)", m.string)
    if cmatch:
        cache_hits = int(cmatch.group(1))
        cache_reads = int(cmatch.group(2))
    session_match = _re.search(r"\[(?P<session>\d{8}_\d{6}_[A-Za-z0-9]+)\]", m.string)
    session_id = session_match.group("session") if session_match else ""
    latency = float(m.group("lat"))
    model = m.group("model")
    cost = _estimate_openrouter_cost(model, in_t, out_t, cache_hits)
    _credit_api_call(
        aid, in_t, out_t, cache_hits, cache_reads,
        model=model,
        provider="openrouter",
        latency_s=latency,
        session_id=session_id,
        call_no=m.group("n"),
    )
    _trace_add(aid, "model.call", f"{model} · {in_t:,} in / {out_t:,} out · {latency}s")
    _trace_usage(aid, in_t, out_t, cost.get("estimated_cost_usd"))
    return {
        "type": "agent_event",
        "agent": aid,
        "ts": _now_hms(),
        "kind": "tool_call",
        "title": f"🧠 model #{m.group('n')} · {m.group('lat')}s · {in_t:,}↑ {out_t:,}↓",
        "full": (f"model: {m.group('model')}\n"
                 f"latency: {m.group('lat')}s\n"
                 f"in/out tokens: {in_t:,} / {out_t:,}\n"
                 + (f"cache: {cache_hits:,}/{cache_reads:,} hit ratio "
                    f"({(100*cache_hits//cache_reads) if cache_reads else 0}%)" if cache_reads else "")),
        "live": True,
    }

_register_pattern(
    r"agent\.conversation_loop:\s+API call #(?P<n>\d+):\s+model=(?P<model>\S+)\s+provider=\S+\s+in=(?P<in_t>\d+)\s+out=(?P<out_t>\d+)\s+total=\d+\s+latency=(?P<lat>[\d.]+)s",
    _api_call_builder,
)

# ── Error / health patterns ────────────────────────────────────────────────────
# These raise/resolve health issues as a side-effect in addition to (optionally)
# returning a feed event. Return None to stay silent in the activity feed.

def _rate_limit_builder(m, aid):
    _raise_issue(f"rate_limit:{aid}", "warning", aid,
                 f"{aid}: rate limited (429)",
                 "Provider is throttling requests — will auto-retry")
    return {
        "type": "agent_event", "agent": aid, "ts": _now_hms(),
        "kind": "tool_error",
        "title": f"⚠️ Rate limited (429) — {aid}",
        "live": True,
    }

_register_pattern(r"(?i)(429|rate.limit|too many requests)", _rate_limit_builder)


def _key_limit_builder(m, aid):
    _raise_issue("openrouter_no_credits", "critical", aid,
                 "OpenRouter API key limit reached",
                 "Key spending cap exceeded — raise the key limit in OpenRouter")
    return {
        "type": "agent_event", "agent": aid, "ts": _now_hms(),
        "kind": "tool_error",
        "title": "🔑 OpenRouter key limit reached",
        "live": True,
    }

_register_pattern(
    r"(?i)(Key limit exceeded|weekly limit\)|HTTP 403: Key limit exceeded)",
    _key_limit_builder,
)


def _auth_error_builder(m, aid):
    _raise_issue(f"auth_error:{aid}", "critical", aid,
                 f"{aid}: auth failure (401/403)",
                 "API key rejected — check OPENROUTER_API_KEY in .env")
    return {
        "type": "agent_event", "agent": aid, "ts": _now_hms(),
        "kind": "tool_error",
        "title": f"🔐 Auth error (401/403) — {aid}",
        "live": True,
    }

_register_pattern(
    r"(?i)(HTTP 401|authentication.fail|api.key.invalid|invalid.api.key|Unauthorized)",
    _auth_error_builder,
)


def _max_tokens_budget_builder(m, aid):
    _raise_issue(f"max_tokens_budget:{aid}", "warning", aid,
                 f"{aid}: OpenRouter max_tokens too high for key budget",
                 "Lower max_tokens or raise the key spending limit")
    return {
        "type": "agent_event", "agent": aid, "ts": _now_hms(),
        "kind": "tool_error",
        "title": f"📉 max_tokens exceeds key budget — {aid}",
        "live": True,
    }

_register_pattern(
    r"(?i)(requires more credits, or fewer max_tokens|can only afford \d+)",
    _max_tokens_budget_builder,
)


def _no_credits_builder(m, aid):
    _raise_issue("openrouter_no_credits", "critical", None,
                 "OpenRouter out of credits",
                 "insufficient_credits error from API — top up at openrouter.ai/credits")
    return {
        "type": "agent_event", "agent": aid, "ts": _now_hms(),
        "kind": "tool_error",
        "title": "💳 Out of credits — OpenRouter",
        "live": True,
    }

_register_pattern(
    r"(?i)(insufficient.credits|out.of.credits|credit.limit.exceeded|HTTP 402(?!.*max_tokens))",
    _no_credits_builder,
)


def _image_gen_fail_builder(m, aid):
    _raise_issue(f"image_gen_fail:{aid}", "warning", aid,
                 f"{aid}: image generation failed",
                 "Check image_gen provider/model config and API key")
    return {
        "type": "agent_event", "agent": aid, "ts": _now_hms(),
        "kind": "tool_error",
        "title": f"🎨 Image generation failed — {aid}",
        "live": True,
    }

_register_pattern(
    r"(?i)(image.gen(?:eration)?.fail|no.image.data|OpenRouter response contained no image|Gemini image generation failed)",
    _image_gen_fail_builder,
)


def _network_error_builder(m, aid):
    _raise_issue(f"network_error:{aid}", "warning", aid,
                 f"{aid}: network/timeout error",
                 "Tool or API call timed out — may auto-retry")
    return {
        "type": "agent_event", "agent": aid, "ts": _now_hms(),
        "kind": "tool_error",
        "title": f"🌐 Network error / timeout — {aid}",
        "live": True,
    }

_register_pattern(
    r"(?i)(connection.timed.out|read.timeout|ConnectTimeout|network.error.*httpx|RemoteDisconnected)",
    _network_error_builder,
)


def _missing_key_builder(m, aid):
    key_name = m.group("key") if "key" in m.groupdict() else "API key"
    _raise_issue(f"missing_key:{key_name}", "critical", aid,
                 f"Missing API key: {key_name}",
                 f"Set {key_name} in {HERMES_DIR}/.env and restart")
    return None  # don't spam feed

_register_pattern(
    r"(?P<key>(?:OPENROUTER|FAL|GEMINI|GOOGLE|OPENAI)_API_KEY)\s+not set",
    _missing_key_builder,
)


def _model_overload_builder(m, aid):
    _raise_issue(f"model_overload:{aid}", "warning", aid,
                 f"{aid}: model overloaded (503/529)",
                 "Provider is overloaded — Hermes will retry with backoff")
    return {
        "type": "agent_event", "agent": aid, "ts": _now_hms(),
        "kind": "tool_error",
        "title": f"⏳ Model overloaded (503/529) — {aid}",
        "live": True,
    }

_register_pattern(
    r"(?i)(HTTP 503|HTTP 529|model.overload|service.unavailable|overloaded)",
    _model_overload_builder,
)


def _context_limit_builder(m, aid):
    _raise_issue(f"context_limit:{aid}", "warning", aid,
                 f"{aid}: context limit hit",
                 "Session context is full — compression or reset needed")
    return {
        "type": "agent_event", "agent": aid, "ts": _now_hms(),
        "kind": "tool_error",
        "title": f"📏 Context limit hit — {aid}",
        "live": True,
    }

_register_pattern(
    r"(?i)(context.length.exceeded|context.window.full|maximum.context|token.limit.exceeded)",
    _context_limit_builder,
)


def _model_error_builder(m, aid):
    session_id = m.group("session")
    trace_incident(aid, "model error", "finish_reason=error", session_id=session_id)
    return {
        "type": "agent_event",
        "agent": aid,
        "ts": _now_hms(),
        "kind": "tool_error",
        "title": f"🚨 model error (finish_reason=error) — {aid}",
        "live": True,
    }


_register_pattern(
    r"agent\.conversation_loop:\s+Turn ended:.*finish_reason=error.*session=(?P<session>\S+)",
    _model_error_builder,
)


def _empty_response_exhausted_builder(m, aid):
    session_id = _session_from_log_line(m.string)
    trace_incident(
        aid, "empty response", "retries exhausted", session_id=session_id, severity="warn",
    )
    return None


def _empty_response_retry_builder(m, aid):
    if m.group("n") != m.group("max"):
        return None
    return _empty_response_exhausted_builder(m, aid)


_register_pattern(
    r"agent\.conversation_loop:\s+Empty response \(no content or reasoning\) — retry (?P<n>\d+)/(?P<max>\d+)",
    _empty_response_retry_builder,
)


def _thinking_only_exhausted_builder(m, aid):
    if m.group("n") != m.group("max"):
        return None
    session_id = _session_from_log_line(m.string)
    trace_incident(
        aid, "empty response", "thinking-only retries exhausted", session_id=session_id,
        severity="warn",
    )
    return None


_register_pattern(
    r"agent\.conversation_loop:\s+Thinking-only response \(no visible content\) — prefilling to continue \((?P<n>\d+)/(?P<max>\d+)\)",
    _thinking_only_exhausted_builder,
)


def _api_retries_exhausted_builder(m, aid):
    session_id = _session_from_log_line(m.string)
    trace_incident(aid, "model error", "API retries exhausted", session_id=session_id)
    return None


_register_pattern(
    r"agent\.conversation_loop:\s+All API retries exhausted",
    _api_retries_exhausted_builder,
)


def _slack_delivery_failed_builder(m, aid):
    detail = m.group("detail") if "detail" in m.groupdict() else m.group(0)[:120]
    trace_incident("monitor", "slack delivery failed", detail[:120])
    return None


_register_pattern(
    r"(?i)(?:gateway\.platforms\.slack|gateway\.run).*(?:send.*failed|SlackApiError|channel_not_found|chat\.postMessage.*failed)",
    _slack_delivery_failed_builder,
)


def _now_hms() -> str:
    """Wall-clock HH:MM:SS for live-feed entries. The activity-feed renderer
    converts this to AM/PM."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _parse_live_log_line(source: str, line: str) -> dict | None:
    """Translate a single log-tail line into a feed event. Returns None for
    lines we don't surface (memory monitor, gateway startup, etc.).

    `source` is one of "gateway:<aid>" or "agent:<aid>" — we extract the
    agent id so the event is attributed correctly even when imagine's log
    is the source.
    """
    if ":" not in source:
        return None
    _, aid = source.split(":", 1)
    if aid not in AGENT_IDS:
        return None
    for rx, builder in _LIVE_LOG_PATTERNS:
        m = rx.search(line)
        if m:
            try:
                return builder(m, aid)
            except Exception:
                return None
    return None


def _tool_call_entry(name: str, args: dict, call_id: str = "") -> dict:
    """Build a feed entry for a generic tool call, pulling the most useful
    field out of `args` so the title reads like the agent's intent, not a
    JSON blob.

    Returns: {kind, title, full} dict.
    """
    full = f"{name}\n\n{json.dumps(args, indent=2, ensure_ascii=False)}"

    def _trunc(v: object, n: int = 90) -> str:
        s = str(v or "").replace("\n", " ")
        return s if len(s) <= n else s[: n - 1] + "…"

    # Map common Hermes tool names to a (icon, summary-extractor) pair.
    # The extractor returns a short human-readable description.
    if name == "terminal":
        cmd = args.get("command") or args.get("cmd") or ""
        first = cmd.strip().splitlines()[0] if cmd else ""
        if "memory.py" in first and " search " in first:
            label = _memory_search_label(first)
            title = f"🧠 memory.search · {label[:100]}"
        else:
            title = f"💻 terminal · {_trunc(first, 120)}"
    elif name == "execute_code":
        code = args.get("code", "")
        lines = code.count("\n") + (1 if code else 0)
        first = code.strip().splitlines()[0] if code else ""
        title = f"🐍 execute_code ({lines}L) · {_trunc(first, 80)}"
    elif name == "read_file":
        p = args.get("path") or args.get("file_path") or ""
        title = f"📖 read · {_trunc(p, 100)}"
    elif name in ("edit_file", "edit", "str_replace_editor"):
        p = args.get("path") or args.get("file_path") or ""
        title = f"✏️ edit · {_trunc(p, 100)}"
    elif name in ("web_search", "search"):
        q = args.get("query") or args.get("q") or ""
        title = f"🔎 tavily.search · {_trunc(q, 100)}"
    elif name in ("web_fetch", "url_fetch", "web_extract"):
        u = args.get("url") or args.get("href") or ""
        title = f"🌐 tavily.extract · {_trunc(u, 100)}"
    elif name == "x_search":
        q = args.get("query") or args.get("q") or args.get("search") or ""
        title = f"𝕏 x.search · {_trunc(q, 100)}"
        full = "x_search\n\n" + json.dumps({
            "query": q,
            "parameters": args,
            "note": "Click result rows or Tool Activity for result previews when available.",
        }, indent=2, ensure_ascii=False)
    elif name == "browser_navigate":
        u = args.get("url") or ""
        title = f"🌐 browser → {_trunc(u, 100)}"
    elif name in ("browser_snapshot", "browser_click", "browser_type", "browser_back"):
        title = f"🌐 {name}"
    elif name == "vision":
        q = args.get("question") or args.get("prompt") or ""
        title = f"👁️ vision · {_trunc(q, 100)}"
    elif name == "clarify":
        q = args.get("question") or args.get("prompt") or ""
        title = f"❓ clarify · {_trunc(q, 100)}"
    elif name in ("memory", "memory_search", "memory_write"):
        q = args.get("query") or args.get("content") or args.get("text") or ""
        op = "search" if "search" in name or args.get("query") else ("write" if "write" in name or args.get("content") else "memory")
        title = f"🧠 memory.{op} · {_trunc(q, 100)}"
    elif name == "todo":
        todos = args.get("todos") or []
        if isinstance(todos, list) and todos:
            active = next((t for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"), None)
            label = (active.get("content") if active else todos[0].get("content")) if todos else ""
            title = f"📋 todo · {_trunc(label, 100)}"
        else:
            title = "📋 todo"
    elif name.startswith("kanban_"):
        op = name.replace("kanban_", "")
        tid = args.get("task_id") or args.get("id") or ""
        extra = args.get("summary") or args.get("body") or args.get("reason") or ""
        title = f"📋 kanban.{op}" + (f" · {_trunc(tid, 14)}" if tid else "") + (f" · {_trunc(extra, 80)}" if extra else "")
    elif name == "skill_view":
        title = f"📘 skill_view · {args.get('name', '')}"
    elif name == "skill_create":
        title = f"📘 skill_create · {args.get('name', '')}"
    elif name == "send_message":
        ch = args.get("channel") or args.get("to") or ""
        msg = args.get("text") or args.get("content") or args.get("message") or ""
        title = f"💬 send_message → {_trunc(ch, 30)} · {_trunc(msg, 80)}"
    else:
        preview = json.dumps(args, ensure_ascii=False)[:90]
        title = f"🔧 {name} {preview}"

    return {"kind": "tool_call", "title": title, "full": full, "call_id": call_id}


def parse_hermes_event(ev: dict) -> list[dict]:
    """Translate one Hermes session jsonl event into 0+ feed entries.

    Hermes session lines have the role inline (no outer "type": "message"
    wrapper). Schemas observed:

      {role: "session_meta",  tools, model, platform, timestamp}
      {role: "user",          content: str, timestamp, message_id}
      {role: "assistant",     content: str, reasoning, tool_calls?: [...], timestamp}
      {role: "tool",          name, tool_name, content, tool_call_id, timestamp}
    """
    role = ev.get("role", "")
    entries: list[dict] = []

    if role == "user":
        text = str(ev.get("content", "") or "").strip()
        if not text:
            return []
        # Hermes prefixes channel messages with "[Display Name] " — strip for display
        display = text
        if text.startswith("[") and "]" in text[:60]:
            close = text.index("]")
            display = text[close + 1 :].lstrip()
        if text.startswith("[Inter-session") or "kanban" in text.lower()[:60]:
            entries.append({
                "kind": "subagent_result",
                "title": "incoming brief / inter-agent",
                "detail": display[:150],
                "full": text,
            })
        else:
            entries.append({
                "kind": "user_message",
                "title": display[:120],
                "full": text,
            })
        return entries

    if role == "assistant":
        text = str(ev.get("content", "") or "").strip()
        if text:
            entries.append({
                "kind": "response",
                "title": text[:120],
                "full": text,
            })
        for tc in ev.get("tool_calls") or []:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            call_id = str(tc.get("id") or tc.get("tool_call_id") or "")
            name = fn.get("name", "tool")
            raw_args = fn.get("arguments", "")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                args = {"_raw": raw_args}

            if name in ("kanban_create", "delegate_task", "sessions_spawn"):
                assignee = args.get("assignee") or args.get("agentId") or args.get("profile") or "subagent"
                brief = args.get("title") or args.get("task") or args.get("description") or args.get("body") or ""
                entries.append({
                    "kind": "delegation",
                    "title": f"→ {assignee}",
                    "detail": str(brief)[:150],
                    "full": json.dumps(args, indent=2, ensure_ascii=False),
                    "call_id": call_id,
                })
            elif name in ("write_file", "write"):
                path = args.get("path") or args.get("file_path") or ""
                entries.append({
                    "kind": "file_write",
                    "title": os.path.basename(path) if path else name,
                    "detail": path,
                    "full": json.dumps(args, indent=2, ensure_ascii=False),
                    "call_id": call_id,
                })
            elif name == "image_generate":
                prompt = args.get("prompt", "")
                entries.append({
                    "kind": "tool_call",
                    "title": f"🎨 image_generate · {str(prompt)[:80]}",
                    "full": json.dumps(args, indent=2, ensure_ascii=False),
                    "call_id": call_id,
                })
            else:
                entries.append(_tool_call_entry(name, args, call_id))
        return entries

    if role == "tool":
        name = ev.get("tool_name") or ev.get("name", "tool")
        content = str(ev.get("content", "") or "")
        call_id = str(ev.get("tool_call_id") or ev.get("id") or "")
        is_error = False
        # Hermes tool results are typically a JSON string; sniff for errors.
        try:
            parsed = json.loads(content) if content.startswith("{") else None
            if isinstance(parsed, dict):
                if parsed.get("error") or parsed.get("isError") or parsed.get("status") == "error":
                    is_error = True
        except Exception:
            pass
        if is_error:
            entries.append({
                "kind": "tool_error",
                "title": name,
                "detail": content[:150],
                "full": content,
                "call_id": call_id,
            })
        else:
            title = f"{name} result"
            try:
                parsed = json.loads(content) if content.startswith("{") else None
                if isinstance(parsed, dict):
                    output = parsed.get("output") or parsed.get("result") or parsed.get("content")
                    if output:
                        title = f"{name} result · {str(output).replace(chr(10), ' ')[:90]}"
            except Exception:
                pass
            entries.append({
                "kind": "tool_result",
                "title": title,
                "detail": content[:150],
                "full": content,
                "call_id": call_id,
            })
        return entries

    return []


# ── Background watcher ─────────────────────────────────────────────────────────

class Watcher(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        # agent_id -> (file_path, byte_offset)  — tracks which file the offset belongs to
        self.session_cursors: dict[str, tuple[str, int]] = {}
        self.log_offsets: dict[str, int] = {}        # source -> byte offset
        # agent_id -> last messages.id we've emitted from each profile's state.db.
        # Hermes stores ALL sessions in state.db (SQLite); jsonl files are only
        # written for some sources (slack/discord/cron). Kanban-spawned workers
        # (source='cli') skip jsonl entirely, so without polling state.db we'd
        # never see Recon/Ink/Imagine activity in the feed.
        self.state_cursors: dict[str, int] = {}
        self._warmup()

    def _warmup(self):
        """Seed the recent-events buffer with the tail of each session so a
        freshly-loaded dashboard reflects current state. Fast-forwards the
        live cursor past the warmup tail so the watcher itself doesn't
        re-emit them when polling kicks in.

        Skips replay for sessions whose latest file is older than
        WARMUP_FRESHNESS_SECONDS — stale sessions still have their cursor
        pinned (so we don't re-process them when polling), but no events
        from yesterday end up cluttering the feed.
        """
        WARMUP_TAIL = 50            # last N events per agent to seed
        WARMUP_FRESHNESS_SECONDS = int(
            os.environ.get("HERMES_WEBUI_WARMUP_FRESHNESS", "21600")
        )  # 6h default — anything older than this is "stale, don't replay"

        now = time.time()
        all_agents = list(AGENT_IDS)
        for aid in all_agents:
            f = latest_session_file(aid)
            if not f:
                continue
            try:
                size = os.path.getsize(f)
                mtime = os.path.getmtime(f)
            except OSError:
                self.session_cursors[aid] = (f, 0)
                continue
            self.session_cursors[aid] = (f, size)

            # Skip warmup-replay if the session is stale. We still pin the
            # cursor (above) so the watcher doesn't replay it later either —
            # only NEW writes after now will surface.
            if now - mtime > WARMUP_FRESHNESS_SECONDS:
                continue

            # Read the file and replay the last N events into the buffer
            # silently (no SSE fan-out — no clients yet anyway).
            try:
                with open(f, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            lines = data.decode("utf-8", errors="replace").splitlines()
            recent_lines = [ln for ln in lines if ln.strip()][-WARMUP_TAIL:]
            for line in recent_lines:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._process_session_event(aid, ev)
        for aid, paths in AGENT_LIVE_LOGS.items():
            for log_type, path in paths.items():
                source = f"{log_type}:{aid}"
                try:
                    self.log_offsets[source] = os.path.getsize(path) if os.path.exists(path) else 0
                except OSError:
                    self.log_offsets[source] = 0

        # state.db warmup: prime cursor, replay last WARMUP_TAIL rows from
        # non-jsonl sessions (worker / cli / cron). Sage's slack sessions are
        # excluded so we don't dupe with the jsonl path.
        STATE_DB_WARMUP_TAIL = 50
        for aid in all_agents:
            db_path = _state_db_path(aid)
            if not os.path.isfile(db_path):
                continue
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT m.id, m.session_id, m.role, m.content,
                              m.tool_call_id, m.tool_calls, m.tool_name,
                              m.timestamp, m.finish_reason,
                              s.source AS sess_source, s.started_at
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       WHERE s.source NOT IN ('slack','discord','telegram','tui')
                       ORDER BY m.id DESC LIMIT ?""",
                    (STATE_DB_WARMUP_TAIL,),
                ).fetchall()
                max_id_row = conn.execute("SELECT MAX(id) FROM messages").fetchone()
                conn.close()
            except Exception:
                continue
            self.state_cursors[aid] = (max_id_row[0] or 0) if max_id_row else 0
            # Replay in chronological order so the dashboard shows oldest→newest
            for row in reversed(rows):
                if now - (row["timestamp"] or 0) > WARMUP_FRESHNESS_SECONDS:
                    continue
                ev = _state_row_to_event(row)
                self._process_session_event(aid, ev)

        # Pre-populate file registry without broadcasting (recent files only)
        self._scan_delivery_files(broadcast_event=False)

    def run(self):
        heartbeat_ts = time.time()
        kanban_ts = 0.0
        state_db_ts = 0.0
        session_usage_ts = 0.0
        health_ts = 0.0
        while True:
            try:
                self._poll_agents()       # jsonl tail
                self._poll_files()
                self._poll_logs()         # gateway.log + agent.log live-tail
                now = time.time()
                if now - state_db_ts >= 1.0:
                    self._poll_state_db()
                    state_db_ts = now
                if now - session_usage_ts >= 3.0:
                    self._poll_session_usage()
                    session_usage_ts = now
                if now - kanban_ts >= 2.0:
                    self._poll_kanban()
                    kanban_ts = now
                # Health checks: 10s cadence — balance check, stalled tasks,
                # gateway state. Credit check internally throttles to 60s.
                if now - health_ts >= 10.0:
                    self._poll_health()
                    health_ts = now
                if now - heartbeat_ts >= 15:
                    broadcast({"type": "heartbeat"})
                    heartbeat_ts = now
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)

    def _poll_session_usage(self):
        """Reconcile completed SessionDB rollups into the monitor ledger.

        Live agent.log parsing gives low latency, but CLI/one-shot runs can miss
        the monitor's live tail window. SessionDB is the durable source for
        completed token/cost rollups, so this pass closes that gap.
        """
        recent_cutoff = time.time() - 24 * 3600
        for aid in AGENT_IDS:
            db_path = _state_db_path(aid)
            if not os.path.isfile(db_path):
                continue
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT id, source, title, model, billing_provider, billing_base_url, started_at, ended_at,
                           input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                           estimated_cost_usd, actual_cost_usd, cost_status, cost_source,
                           pricing_version, api_call_count
                    FROM sessions
                    WHERE started_at >= ?
                      AND (api_call_count > 0 OR input_tokens > 0 OR output_tokens > 0)
                    ORDER BY ended_at DESC
                    LIMIT 100
                    """,
                    (recent_cutoff,),
                ).fetchall()
                conn.close()
            except Exception:
                continue
            for r in rows:
                provider = (r["billing_provider"] or "openrouter").lower()
                if provider != "openrouter" and "openrouter.ai" not in str(r["billing_base_url"] or ""):
                    continue
                _insert_usage_event({
                    "dedupe_key": _usage_dedupe_key("session", aid, r["id"]),
                    "ts": r["ended_at"] or r["started_at"] or time.time(),
                    "prompt_id": str(r["id"]),
                    "agent": aid,
                    "provider": "openrouter",
                    "kind": "session_rollup",
                    "model": r["model"] or "",
                    "input_tokens": r["input_tokens"] or 0,
                    "output_tokens": r["output_tokens"] or 0,
                    "cache_read_tokens": r["cache_read_tokens"] or 0,
                    "cache_write_tokens": r["cache_write_tokens"] or 0,
                    "estimated_cost_usd": r["actual_cost_usd"] if r["actual_cost_usd"] is not None else r["estimated_cost_usd"],
                    "cost_status": r["cost_status"] or ("actual" if r["actual_cost_usd"] is not None else "estimated"),
                    "cost_source": r["cost_source"] or "session_db",
                    "usage_units": r["api_call_count"] or 1,
                    "session_id": r["id"],
                    "detail": r["title"] or r["source"] or "session",
                    "extra": {"source": r["source"], "pricing_version": r["pricing_version"]},
                })

    def _poll_state_db(self):
        """Tail the messages table of each profile's state.db for events from
        non-jsonl session sources (cli, cron, kanban workers). Dedup-safe
        with the jsonl path because we filter out sources that produce jsonl.
        """
        for aid in AGENT_IDS:
            db_path = _state_db_path(aid)
            if not os.path.isfile(db_path):
                continue
            cursor = self.state_cursors.get(aid, 0)
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """SELECT m.id, m.session_id, m.role, m.content,
                              m.tool_call_id, m.tool_calls, m.tool_name,
                              m.timestamp, m.finish_reason,
                              s.source AS sess_source
                       FROM messages m
                       JOIN sessions s ON m.session_id = s.id
                       WHERE m.id > ?
                         AND s.source NOT IN ('slack','discord','telegram','tui')
                       ORDER BY m.id ASC LIMIT 200""",
                    (cursor,),
                ).fetchall()
                conn.close()
            except Exception:
                continue
            for row in rows:
                ev = _state_row_to_event(row)
                self._process_session_event(aid, ev)
                self.state_cursors[aid] = row["id"]

    def _poll_agents(self):
        all_agents = list(AGENT_IDS)
        for aid in all_agents:
            f = latest_session_file(aid)
            if not f:
                continue

            try:
                size = os.path.getsize(f)
            except OSError:
                continue

            prev_file, prev_offset = self.session_cursors.get(aid, (None, 0))

            if f != prev_file:
                # New session file — read from the beginning
                prev_offset = 0
                self.session_cursors[aid] = (f, 0)

            if size <= prev_offset:
                continue

            try:
                with open(f, "rb") as fh:
                    fh.seek(prev_offset)
                    new_data = fh.read(size - prev_offset)
                self.session_cursors[aid] = (f, size)
            except OSError:
                continue

            lines = new_data.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._process_session_event(aid, ev)

    def _process_session_event(self, agent_id: str, ev: dict):
        role = ev.get("role", "")
        if role == "session_meta" or role not in ("user", "assistant", "tool"):
            return

        ts = ev.get("timestamp", "")
        ts_short = ts[:19].replace("T", " ") if ts else ""

        # Dedup key — Hermes events don't carry a guaranteed id, so fall back
        # to (role, timestamp, message_id, content-hash) tuple.
        msg_id = ev.get("message_id") or ev.get("tool_call_id") or ""
        content_for_hash = json.dumps(ev, sort_keys=True, default=str)[:512]
        key = (agent_id, role, ts, msg_id, hash(content_for_hash))
        if key in seen_events:
            return
        seen_events.add(key)

        # Derive a short "session" tag from the current session file basename.
        cursor = self.session_cursors.get(agent_id)
        session_short = None
        if cursor and cursor[0]:
            base = os.path.basename(cursor[0])
            # 20260521_184521_0e5fb3f5.jsonl → 0e5fb3f5
            stem = os.path.splitext(base)[0]
            session_short = stem.split("_")[-1][:8] if "_" in stem else stem[:8]

        # Update agent state and broadcast
        agent_state[agent_id]["last_seen"] = ts_short
        agent_state[agent_id]["active"] = True
        agent_state[agent_id]["session"] = session_short
        broadcast({
            "type": "agent_status",
            "agent": agent_id,
            "active": True,
            "last_seen": ts_short,
            "session": session_short,
        })

        _track_memory_preflight_from_event(agent_id, ev, session_short)

        tool_name = ev.get("tool_name") or ev.get("name", "") if role == "tool" else ""
        if tool_name == "s3_upload":
            content = str(ev.get("content") or "")
            try:
                body = json.loads(content) if content.strip().startswith("{") else {}
                if isinstance(body, dict):
                    lp = body.get("local_path") or ""
                    url = body.get("url") or body.get("s3_url") or ""
                    if lp and url and body.get("success"):
                        _cache_s3_upload(lp, url)
                        _try_register_path(lp, broadcast_event=True)
            except (json.JSONDecodeError, TypeError):
                pass

        for parsed in parse_hermes_event(ev):
            broadcast({
                "type": "agent_event",
                "agent": agent_id,
                "ts": ts_short,
                **parsed,
            })
            kind = parsed.get("kind")
            if kind == "tool_call":
                title = parsed.get("title", "")
                trace_kind = "tool.call"
                if "memory" in title or "memory.py" in (parsed.get("full") or ""):
                    trace_kind = "memory.search"
                elif "tavily" in title or "web_search" in title:
                    trace_kind = "tavily.search"
                elif "x.search" in title or "x_search" in title:
                    trace_kind = "x.search"
                _trace_add(agent_id, trace_kind, title)
            elif kind == "delegation":
                _trace_add(agent_id, "delegation", f"{parsed.get('title','')} · {parsed.get('detail','')}")
            elif kind == "tool_error":
                _trace_add(agent_id, "tool.error", f"{parsed.get('title','')} · {parsed.get('detail','')}")
            elif kind == "file_write":
                fpath = (parsed.get("detail") or "").strip()
                if fpath:
                    _try_register_path(fpath, broadcast_event=True)
            # Trace mirror: milestones mode keeps tool_call quiet; incidents always post.
            if kind == "tool_error":
                title = parsed.get("title", "?")
                detail = (parsed.get("detail") or "")[:120]
                trace_incident(
                    agent_id, "tool_error", f"{title} · {detail}",
                    session_id=session_short,
                )
            elif kind == "delegation" and _trace_verbose():
                em = _agent_emoji(agent_id)
                trace(
                    f"{em} `{agent_id}` · delegated → `{parsed.get('title','?')}` · "
                    f"{parsed.get('detail','')[:120]}"
                )

    def _scan_delivery_files(self, *, broadcast_event: bool) -> int:
        """Walk agent output dirs (+ recent cache images) and register delivery files."""
        registered = 0
        for aid, out_dir in AGENT_OUTPUT_DIRS.items():
            if not os.path.isdir(out_dir):
                continue
            for root, dirs, files in os.walk(out_dir):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for fname in files:
                    if fname.startswith("."):
                        continue
                    fpath = os.path.join(root, fname)
                    if os.path.isfile(fpath) and register_file(
                        aid, fpath, broadcast_event=broadcast_event
                    ):
                        registered += 1
        if FILE_POLL_CACHE_IMAGES and os.path.isdir(MEDIA_DIR):
            try:
                names = os.listdir(MEDIA_DIR)
            except OSError:
                names = []
            for fname in names:
                if fname.startswith("."):
                    continue
                fpath = os.path.join(MEDIA_DIR, fname)
                if os.path.isfile(fpath) and register_file(
                    "imagine", fpath, broadcast_event=broadcast_event
                ):
                    registered += 1
        return registered

    def _poll_files(self):
        self._scan_delivery_files(broadcast_event=True)

    def _poll_kanban(self):
        """Diff kanban board state vs last snapshot. Broadcast a board update if
        any card's status changed, was created, or finished."""
        board = kanban_board_summary()
        if not board.get("available"):
            return
        # Build new snapshot keyed by id → status
        new_snap = {t["id"]: t["status"] for t in board["tasks"]}
        with kanban_snapshot_lock:
            old_snap = dict(kanban_snapshot)
            kanban_snapshot.clear()
            kanban_snapshot.update(new_snap)
        # First-time fill: just broadcast the board without per-card events.
        if not old_snap:
            broadcast({"type": "kanban_board", **board})
            return
        # Compute per-card deltas
        changed_ids = set()
        for tid, status in new_snap.items():
            if old_snap.get(tid) != status:
                changed_ids.add(tid)
        for tid in old_snap:
            if tid not in new_snap:
                # Dropped off the active+recent-done window
                changed_ids.add(tid)
        if changed_ids:
            for t in board["tasks"]:
                if t["id"] in changed_ids:
                    prev = old_snap.get(t["id"])
                    broadcast({
                        "type": "kanban_card_update",
                        "card": t,
                        "prev_status": prev,
                    })
                    self._trace_card_transition(t, prev)
                    if t.get("status") == "done":
                        self._backfill_file_card_ids()
            broadcast({"type": "kanban_board", **board})

        # Raise/resolve issues for blocked cards immediately on board change
        seen_blocked = set()
        for t in board.get("tasks", []):
            if t.get("status") == "blocked":
                tid = t["id"]
                issue_id = f"task_blocked:{tid}"
                seen_blocked.add(issue_id)
                err = t.get("title", "")[:80]
                _raise_issue(issue_id, "critical", t.get("assignee") or None,
                             f"{t.get('assignee','agent')}: task BLOCKED",
                             f"Task {tid}: {err}")
        with issues_lock:
            blocked_ids = [k for k in list(issues_state) if k.startswith("task_blocked:")]
        for k in blocked_ids:
            if k not in seen_blocked:
                _resolve_issue(k)

    def _backfill_file_card_ids(self) -> None:
        """When a card completes, attach its id to any already-registered file
        that matches the card's run.metadata.image_path. Cheap pass — only runs
        on kanban deltas, not on every poll tick."""
        with file_registry_lock:
            unresolved = [e for e in file_registry if not e.get("card_id")]
        if not unresolved:
            return
        for entry in unresolved:
            cid = _find_card_for_file(entry["path"])
            if cid:
                entry["card_id"] = cid
                broadcast({"type": "file_event", **entry})

    def _trace_card_transition(self, card: dict, prev_status: str | None) -> None:
        """Milestone trace posts for workflow boundaries; verbose mode keeps legacy churn."""
        tid = card.get("id", "?")
        title = (card.get("title") or "")[:80]
        assignee = card.get("assignee") or "?"
        status = card.get("status", "?")
        elapsed = card.get("elapsed_s")
        em = _agent_emoji(assignee)

        if not _trace_verbose():
            if prev_status is None:
                if _trace_dedupe_milestone((tid, "created")):
                    return
                trace(f"📋 kanban · `{tid}` created → {em} `{assignee}` · _{title}_")
                _trace_add(HERMES_ORCHESTRATOR, "kanban.created", f"{tid} → {assignee} · {title}")
            elif prev_status in ("ready", "todo", "triage") and status == "running":
                if _trace_dedupe_milestone((tid, "running")):
                    return
                trace(f"⚡ kanban · `{tid}` claimed · {em} `{assignee}`")
                _trace_add(HERMES_ORCHESTRATOR, "kanban.running", f"{tid} claimed by {assignee}")
            elif status == "done":
                if _trace_dedupe_milestone((tid, "done")):
                    return
                detail = _kanban_completion_detail(tid)
                done_text = _format_done_milestone(card, detail)
                trace(done_text)
                _trace_done_incidents(card, detail)
                dur = f"{elapsed}s" if elapsed is not None else "?"
                _trace_add(HERMES_ORCHESTRATOR, "kanban.done", f"{tid} done in {dur} · {assignee}")
            elif status == "blocked":
                if _trace_dedupe_milestone((tid, "blocked")):
                    return
                trace_incident(assignee, "BLOCKED", f"{title} · `{tid}`", severity="blocked")
                _trace_add(HERMES_ORCHESTRATOR, "kanban.blocked", f"{tid} blocked · {assignee} · {title}")
            return

        if prev_status is None:
            trace(f"📋 kanban · `{tid}` created → {em} `{assignee}` · _{title}_")
            _trace_add(HERMES_ORCHESTRATOR, "kanban.created", f"{tid} → {assignee} · {title}")
        elif prev_status in ("ready", "todo", "triage") and status == "running":
            wait = card.get("started_at", 0) - card.get("created_at", 0)
            trace(f"⚡ kanban · `{tid}` claimed by {em} `{assignee}` after `{wait}s` wait")
            _trace_add(HERMES_ORCHESTRATOR, "kanban.running", f"{tid} claimed by {assignee}")
        elif status == "done":
            dur = f"{elapsed}s" if elapsed is not None else "?"
            detail = _kanban_completion_detail(tid)
            if detail:
                trace(_format_done_milestone(card, detail))
                _trace_done_incidents(card, detail)
            else:
                trace(f"✅ kanban · `{tid}` done in `{dur}` · {em} `{assignee}`")
            _trace_add(HERMES_ORCHESTRATOR, "kanban.done", f"{tid} done in {dur} · {assignee}")
        elif status == "blocked":
            trace_incident(assignee, "BLOCKED", f"{title} · `{tid}`", severity="blocked")
            trace(f"⛔ kanban · `{tid}` BLOCKED · {em} `{assignee}` · _{title}_")
            _trace_add(HERMES_ORCHESTRATOR, "kanban.blocked", f"{tid} blocked · {assignee} · {title}")
        else:
            trace(f"📋 kanban · `{tid}` {prev_status} → {status} · {em} `{assignee}`")

    def _poll_health(self):
        """Check all health conditions and raise/resolve issues accordingly."""
        # 1. OpenRouter credits (throttled internally to 60s)
        data = fetch_openrouter_key_status()
        if "error" in data:
            err_code = openrouter_key_state.get("error", "")
            if err_code == "no_key":
                _raise_issue("openrouter_no_key", "critical", None,
                             "OpenRouter API key missing",
                             f"OPENROUTER_API_KEY not set in {HERMES_DIR}/.env")
            else:
                _raise_issue("openrouter_api_error", "warning", None,
                             f"OpenRouter key check failed: {data.get('error', '')}",
                             "Temporary network issue — will retry")
        else:
            _resolve_issue("openrouter_no_key")
            _resolve_issue("openrouter_api_error")

        # 2. Gateway platform state from gateway_state.json
        self._check_gateway_state()

        # 3. Stalled agents — running kanban task with no log activity > 15 min
        self._check_stalled_agents()

        # 4. Auto-resolve transient issues after 5 min of silence
        # (rate limits, network errors, model overload usually self-clear)
        self._expire_transient_issues()

    def _check_gateway_state(self):
        gw_path = os.path.join(HERMES_DIR, "gateway_state.json")
        try:
            with open(gw_path) as f:
                gw = json.load(f)
        except Exception:
            return
        state = gw.get("gateway_state", "")
        if state not in ("running", ""):
            _raise_issue("gateway_state", "critical", None,
                         f"Gateway not running (state: {state})",
                         "Restart via the agent controls or `hermes gateway run`")
        else:
            _resolve_issue("gateway_state")

        # Platform connectivity
        for platform, info in (gw.get("platforms") or {}).items():
            p_state = (info or {}).get("state", "")
            if p_state not in ("connected", ""):
                err = (info or {}).get("error_message") or p_state
                _raise_issue(f"platform_disconnected:{platform}", "warning", None,
                             f"{platform} platform disconnected",
                             err[:120] if err else "")
            else:
                _resolve_issue(f"platform_disconnected:{platform}")

    def _check_stalled_agents(self):
        STALL_THRESHOLD_S = 15 * 60  # 15 minutes
        conn = _kanban_conn()
        if not conn:
            return
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, title, assignee, started_at FROM tasks "
                "WHERE status = 'running' AND started_at IS NOT NULL"
            )
            running = cur.fetchall()
        except sqlite3.Error:
            return
        finally:
            conn.close()

        now = int(time.time())
        seen_stalled = set()
        for tid, title, assignee, started_at in running:
            elapsed = now - (started_at or now)
            if elapsed > STALL_THRESHOLD_S:
                issue_id = f"stalled_task:{tid}"
                seen_stalled.add(issue_id)
                mins = elapsed // 60
                _raise_issue(issue_id, "warning", assignee or None,
                             f"{assignee or 'agent'}: task running {mins}m with no completion",
                             f"Task {tid}: {(title or '')[:80]}")

        # Resolve stall issues for tasks no longer running
        with issues_lock:
            stall_ids = [k for k in list(issues_state) if k.startswith("stalled_task:")]
        for k in stall_ids:
            if k not in seen_stalled:
                _resolve_issue(k)

    # issue_ids that should auto-expire after silence (transient conditions)
    _TRANSIENT_PREFIXES = ("rate_limit:", "network_error:", "model_overload:", "image_gen_fail:")
    _TRANSIENT_TTL_S = 5 * 60  # 5 minutes

    def _expire_transient_issues(self):
        import datetime as _dt
        now_s = time.time()
        with issues_lock:
            candidates = [
                (k, v) for k, v in list(issues_state.items())
                if any(k.startswith(p) for p in self._TRANSIENT_PREFIXES)
            ]
        for k, v in candidates:
            try:
                issue_ts = time.mktime(time.strptime(v["ts"], "%Y-%m-%d %H:%M:%S"))
                if now_s - issue_ts > self._TRANSIENT_TTL_S:
                    _resolve_issue(k)
            except Exception:
                pass

    def _poll_logs(self):
        # Build the polling list dynamically from AGENT_LIVE_LOGS so adding a
        # new specialist just means adding to that dict.
        sources: list[tuple[str, str]] = []
        for aid, paths in AGENT_LIVE_LOGS.items():
            gw = paths.get("gateway")
            if gw:
                sources.append((f"gateway:{aid}", gw))
            sources.append((f"agent:{aid}", paths["agent"]))

        for source, path in sources:
            if not os.path.exists(path):
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue

            prev = self.log_offsets.get(source, 0)
            # Fast-forward on first sight so we don't replay history
            if prev == 0 and size > 0:
                self.log_offsets[source] = size
                continue
            if size <= prev:
                continue

            try:
                with open(path, "rb") as fh:
                    fh.seek(prev)
                    new_data = fh.read(size - prev)
                self.log_offsets[source] = size
            except OSError:
                continue

            lines = new_data.decode("utf-8", errors="replace").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # Try to parse the line into a structured live feed event.
                # Falls back to a plain log_line broadcast for the (now
                # hidden) Logs panel.
                parsed = _parse_live_log_line(source, line)
                if parsed:
                    broadcast(parsed)

                level = "INFO"
                if "[DEBUG" in line or "[DEBUG]" in line:
                    level = "DEBUG"
                elif "[WARN" in line or "[WARNING" in line:
                    level = "WARNING"
                elif "[ERROR" in line:
                    level = "ERROR"
                broadcast({
                    "type": "log_line",
                    "source": source,
                    "level": level,
                    "text": line,
                })


MIME = {
    ".html": "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".ico":  "image/x-icon",
    ".md":   "text/plain; charset=utf-8",
    ".txt":  "text/plain; charset=utf-8",
    ".csv":  "text/plain; charset=utf-8",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".svg":  "image/svg+xml",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress default access log

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        qs = self.path[len(path):]

        if path == "" or path == "/":
            self._serve_file(os.path.join(STATIC_DIR, "index.html"))
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            self._serve_file(os.path.join(STATIC_DIR, rel))
        elif path == "/api/events":
            self._sse()
        elif path == "/api/agents":
            self._send_json({
                "orchestrator": HERMES_ORCHESTRATOR,
                "agents": _agent_metadata_list(),
            })
        elif path == "/api/status":
            self._status()
        elif path == "/api/logs":
            self._logs(qs)
        elif path == "/api/files":
            self._files()
        elif path == "/api/file":
            self._file_content(qs)
        elif path == "/api/kanban":
            self._send_json(kanban_board_summary())
        elif path.startswith("/api/kanban/card/"):
            task_id = path[len("/api/kanban/card/"):]
            self._send_json(kanban_card_detail(task_id))
        elif path == "/api/backup/status":
            self._send_json(backup_status())
        elif path == "/api/usage":
            self._send_json(usage_snapshot())
        elif path == "/api/activity":
            self._activity(qs)
        elif path == "/api/prompt-traces":
            self._send_json(prompt_trace_snapshot())
        elif path == "/api/health":
            self._send_json({"issues": _get_issues()})
        elif path == "/api/info":
            def _get_hermes_version() -> str:
                try:
                    cp = subprocess.run(["hermes", "version"], capture_output=True, text=True, timeout=5)
                    return cp.stdout.strip().split("\n")[0] if cp.returncode == 0 else "unknown"
                except Exception:
                    return "unknown"

            def _get_gateway_pid() -> "int | None":
                state_file = os.path.join(HERMES_DIR, "gateway_state.json")
                try:
                    with open(state_file) as f:
                        data = json.load(f)
                        return data.get("pid")
                except Exception:
                    return None

            worker_pids = {aid: _get_worker_pids(aid) for aid in AGENT_IDS if aid != HERMES_ORCHESTRATOR}
            self._send_json({
                "app": "Hermes Console",
                "hermes_dir": HERMES_DIR,
                "orchestrator": HERMES_ORCHESTRATOR,
                "docker_container": DOCKER_CONTAINER or None,
                "hermes_version": _get_hermes_version(),
                "gateway_pid": _get_gateway_pid(),
                "active_worker_pids": worker_pids,
                "monitor_db": MONITOR_DB,
                "webui_port": int(os.environ.get("HERMES_WEBUI_PORT", 7979)),
            })
        elif path == "/api/cron":
            def _read_cron_db() -> list:
                db_path = os.path.join(HERMES_DIR, "cron.db")
                if not os.path.exists(db_path):
                    return []
                try:
                    conn = sqlite3.connect(db_path, timeout=2)
                    conn.row_factory = sqlite3.Row
                    tables = [r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()]
                    tbl = next((t for t in tables if 'cron' in t.lower() or 'job' in t.lower()), None)
                    if not tbl:
                        conn.close()
                        return []
                    rows = conn.execute(f"SELECT * FROM {tbl} ORDER BY rowid DESC").fetchall()
                    result = [dict(r) for r in rows]
                    conn.close()
                    return result
                except Exception:
                    return []

            try:
                cp = subprocess.run(
                    ["hermes", "cron", "list", "--json"],
                    capture_output=True, text=True, timeout=10,
                    env={**os.environ, "HERMES_HOME": HERMES_DIR},
                )
                if cp.returncode == 0 and cp.stdout.strip().startswith('['):
                    self._send_json({"jobs": json.loads(cp.stdout)})
                elif cp.returncode == 0 and cp.stdout.strip().startswith('{'):
                    parsed = json.loads(cp.stdout)
                    self._send_json({"jobs": parsed.get("jobs", parsed.get("data", []))})
                else:
                    raise RuntimeError("cli output not json")
            except Exception:
                self._send_json({"jobs": _read_cron_db()})
        elif path == "/api/settings":
            self._send_json({
                # Connection
                "hermes_dir":         HERMES_DIR,
                "orchestrator":       HERMES_ORCHESTRATOR,
                "docker_container":   DOCKER_CONTAINER,
                "webui_port":         PORT,
                "extra_home":         HERMES_EXTRA_HOME,
                # Monitoring
                "trace_mode":         TRACE_MODE,
                "slack_trace_channel": SLACK_TRACE_CHANNEL,
                "warmup_freshness":   int(os.environ.get("HERMES_WEBUI_WARMUP_FRESHNESS", 21600)),
                # Backup
                "backup_repo":        BACKUP_REPO or "",
                "backup_script":      BACKUP_SCRIPT or "",
                # Data
                "vector_db_url":      os.environ.get("HERMES_LOG_DB_URL", ""),
                "files_max_age_days": int(os.environ.get("HERMES_FILES_MAX_AGE_DAYS", 14)),
                "files_max_entries":  int(os.environ.get("HERMES_FILES_MAX_ENTRIES", 80)),
                # Read-only status indicators
                "trace_enabled":      TRACE_ENABLED,
                "backup_configured":  bool(BACKUP_REPO and BACKUP_SCRIPT and os.path.isfile(BACKUP_SCRIPT)),
            })
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/backup/run":
            self._send_json(run_backup())
        elif path == "/api/kanban/archive-done":
            self._send_json(kanban_archive_done())
        elif path.startswith("/api/kanban/card/") and path.endswith("/archive"):
            task_id = path[len("/api/kanban/card/"):-len("/archive")]
            self._send_json(kanban_archive_one(task_id))
        elif path.startswith("/api/kanban/card/") and path.endswith("/cancel"):
            task_id = path[len("/api/kanban/card/"):-len("/cancel")]
            self._send_json(kanban_cancel_one(task_id))
        elif path.startswith("/api/agent/"):
            # /api/agent/<id>/<start|stop|restart>
            rest = path[len("/api/agent/"):].strip("/").split("/")
            if len(rest) == 2:
                agent_id, action = rest
                self._send_json(agent_lifecycle(agent_id, action))
            else:
                self._send_json({"error": "expected /api/agent/<id>/<action>"}, 400)
        elif path.startswith("/api/cron/"):
            parts = path.strip("/").split("/")
            # parts = ["api", "cron", "<id>", "<action>"]
            if len(parts) == 4:
                cron_id, cron_action = parts[2], parts[3]
                if cron_action not in ("pause", "resume", "run"):
                    self._send_json({"error": f"unknown cron action: {cron_action}"}, 400)
                else:
                    try:
                        cp = subprocess.run(
                            ["hermes", "cron", cron_action, cron_id],
                            capture_output=True, text=True, timeout=15,
                            env={**os.environ, "HERMES_HOME": HERMES_DIR},
                        )
                        self._send_json({"ok": cp.returncode == 0, "action": cron_action,
                                         "id": cron_id, "msg": (cp.stderr or cp.stdout).strip()})
                    except Exception as exc:
                        self._send_json({"ok": False, "error": str(exc)}, 500)
            else:
                self._send_json({"error": "expected /api/cron/<id>/<action>"}, 400)
        elif path == "/api/settings":
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
            env_local = os.path.join(SCRIPT_DIR, ".env.local")
            KEY_MAP = {
                "hermes_dir":          "HERMES_DIR",
                "orchestrator":        "HERMES_ORCHESTRATOR",
                "docker_container":    "HERMES_DOCKER_CONTAINER",
                "webui_port":          "HERMES_WEBUI_PORT",
                "extra_home":          "HERMES_EXTRA_HOME",
                "trace_mode":          "HERMES_TRACE_MODE",
                "slack_trace_channel": "SLACK_TRACE_CHANNEL",
                "warmup_freshness":    "HERMES_WEBUI_WARMUP_FRESHNESS",
                "backup_repo":         "HERMES_BACKUP_REPO",
                "backup_script":       "HERMES_BACKUP_SCRIPT",
                "vector_db_url":       "HERMES_LOG_DB_URL",
                "files_max_age_days":  "HERMES_FILES_MAX_AGE_DAYS",
                "files_max_entries":   "HERMES_FILES_MAX_ENTRIES",
            }
            # Read existing .env.local so saving one tab doesn't wipe other tabs' keys
            existing: dict[str, str] = {}
            if os.path.isfile(env_local):
                with open(env_local) as _f:
                    for _line in _f:
                        _line = _line.strip()
                        if _line and not _line.startswith("#") and "=" in _line:
                            _k, _, _v = _line.partition("=")
                            existing[_k.strip()] = _v.strip()
            updated = 0
            for k, v in data.items():
                env_key = KEY_MAP.get(k)
                if env_key is None:
                    continue
                val = str(v).strip()
                if val:
                    existing[env_key] = val
                else:
                    existing.pop(env_key, None)
                updated += 1
            with open(env_local, "w") as _f:
                _f.write("# Written by Hermes Console — settings panel\n")
                for _k, _v in existing.items():
                    _f.write(f"{_k}={_v}\n")
            self._send_json({"ok": True, "restart_required": True, "written": updated})
        else:
            self._send_json({"error": "not found"}, 404)

    def _serve_file(self, fpath: str):
        if not os.path.isfile(fpath):
            self._send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(fpath)[1]
        mime = MIME.get(ext, "application/octet-stream")
        try:
            with open(fpath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self._send_json({"error": "read error"}, 500)

    def _send_json(self, data: dict, code: int = 200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _status(self):
        agents_out = {}
        for aid in AGENT_IDS:
            s = agent_state.get(aid, {})
            agents_out[aid] = {
                "active": s.get("active", False),
                "last_seen": s.get("last_seen"),
                "session": s.get("session"),
                "gateway_running": _agent_running(aid),
                "has_gateway": (aid == HERMES_ORCHESTRATOR) if DOCKER_CONTAINER else (aid in AGENT_PLISTS),
                "pids": (_get_worker_pids(aid) if aid != HERMES_ORCHESTRATOR else []),
                "worker_count": (len(_get_worker_pids(aid)) if aid != HERMES_ORCHESTRATOR else 0),
            }
        board = kanban_board_summary()
        self._send_json({
            "agents": agents_out,
            "kanban": {"counts": board.get("counts", {}), "available": board.get("available", False)},
        })

    def _files(self):
        with file_registry_lock:
            entries = list(reversed(file_registry))  # newest first
        self._send_json({"files": entries})

    def _activity(self, qs: str):
        params = {}
        if qs.startswith("?"):
            for part in qs[1:].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
        try:
            limit = int(params.get("limit", 80))
        except ValueError:
            limit = 80
        self._send_json(activity_snapshot(limit))

    def _file_content(self, qs: str):
        params = {}
        if qs.startswith("?"):
            for part in qs[1:].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
        path = params.get("path", "")
        if not path or not _is_allowed_file_path(path):
            self._send_json({"error": "forbidden"}, 403)
            return
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        # Dereference binary-content placeholder stubs → actual file in media/
        path = _resolve_file_path(path)
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        mime = MIME.get(ext, "text/plain; charset=utf-8")
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f'inline; filename="{os.path.basename(path)}"')
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self._send_json({"error": "read error"}, 500)

    def _logs(self, qs: str):
        params = {}
        if qs.startswith("?"):
            for part in qs[1:].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k] = v
        source = params.get("source", "gateway")
        try:
            n = int(params.get("lines", 200))
        except ValueError:
            n = 200
        path_map = {aid: AGENT_LIVE_LOGS[aid].get("gateway", AGENT_LIVE_LOGS[aid]["agent"])
                    for aid in AGENT_LIVE_LOGS}
        path_map.setdefault("gateway", GATEWAY_LOG)   # keep legacy "gateway" key
        path = path_map.get(source, GATEWAY_LOG)      # fallback to orchestrator log
        lines = read_last_lines(path, n)
        self._send_json({"lines": lines, "source": source})

    def _sse(self):
        q: queue.Queue = queue.Queue(maxsize=500)
        with sse_lock:
            sse_clients.append(q)

        self.send_response(200)
        self.send_cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Send initial status + replay recent events so a freshly-loaded
        # dashboard reflects current state instead of a blank slate.
        try:
            # Current health issues
            issues_init = json.dumps({"type": "health_init", "issues": _get_issues()})
            self.wfile.write(f"data: {issues_init}\n\n".encode())
            # Current kanban board snapshot
            board = kanban_board_summary()
            self.wfile.write(f"data: {json.dumps({'type': 'kanban_board', **board})}\n\n".encode())
            self.wfile.write(f"data: {json.dumps({'type': 'usage_update', **usage_snapshot()})}\n\n".encode())
            self.wfile.write(f"data: {json.dumps({'type': 'prompt_trace_init', **prompt_trace_snapshot()})}\n\n".encode())
            for aid in AGENT_IDS:
                s = agent_state.get(aid, {})
                msg = json.dumps({
                    "type": "agent_status",
                    "agent": aid,
                    "active": s.get("active", False),
                    "last_seen": s.get("last_seen"),
                    "session": s.get("session"),
                })
                self.wfile.write(f"data: {msg}\n\n".encode())
            with recent_events_lock:
                backlog = list(recent_events)
            if backlog:
                marker = json.dumps({"type": "backlog_begin", "count": len(backlog)})
                self.wfile.write(f"data: {marker}\n\n".encode())
                for ev in backlog:
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                end = json.dumps({"type": "backlog_end"})
                self.wfile.write(f"data: {end}\n\n".encode())
            self.wfile.flush()
        except Exception:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)
            return

        try:
            while True:
                try:
                    payload = q.get(timeout=20)
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                except queue.Empty:
                    # Keep-alive comment
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)


# ── Main ───────────────────────────────────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    # Start the Slack trace mirror worker if configured. Daemon thread so it
    # exits cleanly on Ctrl+C / launchd stop.
    if TRACE_ENABLED:
        threading.Thread(target=_trace_worker, daemon=True, name="trace-worker").start()
        trace("🟢 hermes-monitor · trace mirror online")

    watcher = Watcher()
    watcher.start()

    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Hermes Console running at http://localhost:{PORT}")
    print(f"  Hermes home : {HERMES_DIR}")
    if HERMES_EXTRA_HOME:
        print(f"  Extra home  : {HERMES_EXTRA_HOME}  (Docker lab)")
    print(f"  Profiles    : {PROFILES_DIR}")
    print(f"  Workspaces  : {WORKSPACE_DIR}")
    print(f"  Logs dir    : {LOGS_DIR}")
    print(f"  Static dir  : {STATIC_DIR}")
    print(f"  Agents      : {', '.join(AGENT_IDS)}")
    extra = [a for a in AGENT_IDS if a in _AGENT_HOME_MAP]
    if extra:
        print(f"  Lab agents  : {', '.join(extra)}")
    print(f"  Trace mirror: {'ENABLED → ' + SLACK_TRACE_CHANNEL if TRACE_ENABLED else f'disabled (set SLACK_TRACE_CHANNEL + SLACK_BOT_TOKEN in {HERMES_DIR}/.env)'}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
