from __future__ import annotations

"""
hermes_console.parsers.session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Parse Hermes session jsonl lines and state.db rows into normalised feed dicts.

These functions are called from the Watcher's poll loops.  They produce lists
of ``{kind, title, ...}`` dicts that are broadcast directly on SSE.
"""

import datetime
import glob
import json
import os
import time

from hermes_console.config import AGENT_IDS, _agent_root

# ── Feed-tool dedup (prevents jsonl ↔ log-tail double-posting) ────────────────
# Shared mutable state — only the Watcher and the log-pattern builders write to
# these dicts, so no lock is needed (single-thread access).

_FEED_TOOL_DEDUPE_TTL_S = 120
_feed_tool_dedupe: dict[tuple, float] = {}

# Tools whose lifecycle is already tracked via session jsonl / state.db.
# Live-log pattern builders return None for these to avoid duplication.
STRUCTURED_LOG_TOOLS = frozenset({
    "web_search", "web_fetch", "web_extract", "x_search",
    "tavily_search", "tavily_extract",
    "read_file", "terminal", "execute_code",
    "browser_navigate", "browser_snapshot", "browser_click", "browser_type", "browser_back",
    "search_files", "session_search", "memory_search", "memory_write", "memory",
})

# call_id → {name, detail, agent} — enriches tool_result feed titles with intent.
_tool_call_context: dict[str, dict] = {}


def _feed_tool_dedupe_key(agent_id: str, kind: str, tool_name: str, detail: str) -> tuple:
    return (agent_id, kind, tool_name, detail.strip().lower()[:160])


def _prune_feed_tool_dedupe(now: float | None = None) -> None:
    now = now or time.time()
    stale = [k for k, ts in list(_feed_tool_dedupe.items()) if now - ts > _FEED_TOOL_DEDUPE_TTL_S]
    for k in stale:
        _feed_tool_dedupe.pop(k, None)


def _feed_tool_recently_emitted(agent_id: str, kind: str, tool_name: str, detail: str) -> bool:
    if not detail:
        return False
    now = time.time()
    _prune_feed_tool_dedupe(now)
    key = _feed_tool_dedupe_key(agent_id, kind, tool_name, detail)
    return now - _feed_tool_dedupe.get(key, 0) < _FEED_TOOL_DEDUPE_TTL_S


def _mark_feed_tool_emitted(agent_id: str, kind: str, tool_name: str, detail: str) -> None:
    if not detail:
        return
    now = time.time()
    _prune_feed_tool_dedupe(now)
    key = _feed_tool_dedupe_key(agent_id, kind, tool_name, detail)
    _feed_tool_dedupe[key] = now


# ── Tool call context ──────────────────────────────────────────────────────────

def _remember_tool_call(agent_id: str, call_id: str, name: str, args: dict) -> None:
    if not call_id:
        return
    detail = _tool_intent_detail(name, args if isinstance(args, dict) else {})
    if detail:
        _tool_call_context[call_id] = {"name": name, "detail": detail, "agent": agent_id}


def _pop_tool_call_context(call_id: str) -> dict | None:
    if not call_id:
        return None
    return _tool_call_context.pop(call_id, None)


def _credit_structured_tool_call(agent_id: str, name: str, args: dict, session_id: str = "") -> None:
    from hermes_console.services.usage import _credit_external_tool
    if name in ("web_search", "search", "tavily_search"):
        query = str(args.get("query") or args.get("q") or "")
        if query:
            _credit_external_tool("tavily", agent_id, "tavily_search", query,
                                  session_id=session_id, detail=f"Tavily search: {query}")
    elif name in ("web_fetch", "web_extract", "tavily_extract"):
        url = str(args.get("url") or args.get("href") or args.get("link") or "")
        if url:
            _credit_external_tool("tavily", agent_id, "tavily_extract", url,
                                  session_id=session_id, detail=f"Tavily extract: {url}")
    elif name == "x_search":
        query = str(args.get("query") or args.get("q") or args.get("search") or "")
        if query:
            _credit_external_tool("x", agent_id, "x_search", query,
                                  session_id=session_id, detail=f"X search: {query}")


# ── Session file discovery ─────────────────────────────────────────────────────

def _state_db_path(agent_id: str) -> str:
    return os.path.join(_agent_root(agent_id), "state.db")


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


def _state_row_to_event(row) -> dict:
    """Translate a state.db ``messages`` row into the dict shape that
    ``parse_hermes_event()`` expects."""
    ts_iso = ""
    try:
        ts = float(row["timestamp"] or 0)
        if ts > 0:
            ts_iso = datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    except Exception:
        pass

    tool_calls = None
    raw_tc     = row["tool_calls"]
    if raw_tc:
        try:
            tool_calls = json.loads(raw_tc) if isinstance(raw_tc, str) else raw_tc
        except Exception:
            tool_calls = None

    return {
        "role":          row["role"],
        "content":       row["content"] or "",
        "timestamp":     ts_iso,
        "message_id":    f"sqlite:{row['id']}",
        "session_id":    row["session_id"] or "",
        "tool_call_id":  row["tool_call_id"] or "",
        "tool_calls":    tool_calls,
        "tool_name":     row["tool_name"],
        "name":          row["tool_name"],
        "finish_reason": row["finish_reason"],
    }


# ── Tool display helpers ───────────────────────────────────────────────────────

def _trunc_feed(value: object, limit: int = 90) -> str:
    s = str(value or "").replace("\n", " ").strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _tool_intent_detail(name: str, args: dict) -> str:
    """Short human-readable intent string for a tool invocation."""
    if name == "terminal":
        cmd   = args.get("command") or args.get("cmd") or ""
        first = cmd.strip().splitlines()[0] if cmd else ""
        from hermes_console.services.slack_trace import _memory_search_label
        if "memory.py" in first and " search " in first:
            return _memory_search_label(first)[:160]
        return _trunc_feed(first, 160)
    if name in ("web_search", "search", "x_search", "tavily_search"):
        return _trunc_feed(args.get("query") or args.get("q") or args.get("search") or "", 160)
    if name in ("web_fetch", "url_fetch", "web_extract", "tavily_extract"):
        return _trunc_feed(args.get("url") or args.get("href") or args.get("link") or "", 160)
    if name in ("read_file", "edit_file", "edit", "str_replace_editor", "write_file", "write"):
        return _trunc_feed(args.get("path") or args.get("file_path") or "", 160)
    if name == "search_files":
        pattern = args.get("pattern") or args.get("query") or ""
        path    = args.get("path") or ""
        detail  = _trunc_feed(pattern, 100)
        if path:
            detail = f"{detail} in {_trunc_feed(path, 60)}"
        return detail
    if name in ("session_search", "memory_search", "memory"):
        return _trunc_feed(args.get("query") or args.get("q") or args.get("content") or "", 160)
    if name == "browser_navigate":
        return _trunc_feed(args.get("url") or "", 160)
    if name == "execute_code":
        code = args.get("code", "")
        return _trunc_feed(code.strip().splitlines()[0] if code else "", 160)
    if name in ("vision", "clarify"):
        return _trunc_feed(args.get("question") or args.get("prompt") or "", 160)
    if name.startswith("kanban_"):
        tid   = args.get("task_id") or args.get("id") or ""
        extra = args.get("summary") or args.get("body") or args.get("reason") or ""
        bits  = [b for b in (_trunc_feed(tid, 20), _trunc_feed(extra, 100)) if b]
        return " · ".join(bits)
    if name in ("skill_view", "skill_create"):
        return _trunc_feed(args.get("name") or "", 160)
    if name == "send_message":
        ch  = args.get("channel") or args.get("to") or ""
        msg = args.get("text") or args.get("content") or args.get("message") or ""
        return _trunc_feed(f"{ch}: {msg}" if ch else msg, 160)
    if name == "image_generate":
        return _trunc_feed(args.get("prompt") or "", 160)
    if name == "todo":
        todos = args.get("todos") or []
        if isinstance(todos, list) and todos:
            active = next((t for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"), None)
            label  = (active.get("content") if active else todos[0].get("content")) if todos else ""
            return _trunc_feed(label, 160)
    return json.dumps(args, ensure_ascii=False)[:120]


def _tool_call_entry(name: str, args: dict, call_id: str = "") -> dict:
    """Build a feed entry for a generic tool call."""
    full   = f"{name}\n\n{json.dumps(args, indent=2, ensure_ascii=False)}"
    detail = _tool_intent_detail(name, args)

    if name == "terminal":
        cmd   = args.get("command") or args.get("cmd") or ""
        first = cmd.strip().splitlines()[0] if cmd else ""
        from hermes_console.services.slack_trace import _memory_search_label
        if "memory.py" in first and " search " in first:
            title = f"🧠 memory.search · {detail[:100]}"
        else:
            title = f"💻 terminal · {detail[:120]}"
    elif name == "execute_code":
        code  = args.get("code", "")
        lines = code.count("\n") + (1 if code else 0)
        title = f"🐍 execute_code ({lines}L) · {detail[:80]}"
    elif name == "read_file":
        title = f"📖 read · {detail[:100]}"
    elif name in ("edit_file", "edit", "str_replace_editor"):
        title = f"✏️ edit · {detail[:100]}"
    elif name in ("web_search", "search"):
        title = f"🔎 tavily.search · {detail[:100]}"
    elif name in ("web_fetch", "url_fetch", "web_extract"):
        title = f"🌐 tavily.extract · {detail[:100]}"
    elif name == "x_search":
        title = f"𝕏 x.search · {detail[:100]}"
        full  = "x_search\n\n" + json.dumps({
            "query":      detail,
            "parameters": args,
            "note":       "Click result rows or Tool Activity for result previews when available.",
        }, indent=2, ensure_ascii=False)
    elif name == "browser_navigate":
        title = f"🌐 browser → {detail[:100]}"
    elif name in ("browser_snapshot", "browser_click", "browser_type", "browser_back"):
        title = f"🌐 {name}" + (f" · {detail[:80]}" if detail else "")
    elif name in ("vision", "clarify"):
        icon  = "👁️" if name == "vision" else "❓"
        title = f"{icon} {name} · {detail[:100]}"
    elif name == "search_files":
        title = f"🔎 search_files · {detail[:100]}"
    elif name in ("session_search", "memory_search"):
        title = f"🔍 recall · {detail[:100]}"
    elif name in ("memory", "memory_write"):
        op    = "search" if "search" in name or args.get("query") else ("write" if "write" in name or args.get("content") else "memory")
        title = f"🧠 memory.{op} · {detail[:100]}"
    elif name == "todo":
        title = f"📋 todo · {detail[:100]}" if detail else "📋 todo"
    elif name.startswith("kanban_"):
        op    = name.replace("kanban_", "")
        title = f"📋 kanban.{op}" + (f" · {detail[:100]}" if detail else "")
    elif name in ("skill_view", "skill_create"):
        title = f"📘 {name} · {args.get('name', '')}"
    elif name == "send_message":
        title = f"💬 send_message · {detail[:100]}"
    elif name == "image_generate":
        title = f"🎨 image_generate · {detail[:80]}"
    else:
        preview = json.dumps(args, ensure_ascii=False)[:90]
        title   = f"🔧 {name} {preview}"

    return {
        "kind":    "tool_call",
        "title":   title,
        "detail":  detail,
        "tool":    name,
        "full":    full,
        "call_id": call_id,
    }


# ── Main parser ────────────────────────────────────────────────────────────────

def parse_hermes_event(ev: dict) -> list[dict]:
    """Translate one Hermes session jsonl event into 0+ feed dicts.

    Schemas observed::

      {role: "session_meta",  tools, model, platform, timestamp}
      {role: "user",          content: str, timestamp, message_id}
      {role: "assistant",     content: str, reasoning, tool_calls?: [...], timestamp}
      {role: "tool",          name, tool_name, content, tool_call_id, timestamp}
    """
    role    = ev.get("role", "")
    entries: list[dict] = []

    if role == "user":
        text = str(ev.get("content", "") or "").strip()
        if not text:
            return []
        display = text
        if text.startswith("[") and "]" in text[:60]:
            close   = text.index("]")
            display = text[close + 1:].lstrip()
        if text.startswith("[Inter-session") or "kanban" in text.lower()[:60]:
            entries.append({
                "kind":   "subagent_result",
                "title":  "incoming brief / inter-agent",
                "detail": display[:150],
                "full":   text,
            })
        else:
            entries.append({
                "kind":  "user_message",
                "title": display[:120],
                "full":  text,
            })
        return entries

    if role == "assistant":
        text = str(ev.get("content", "") or "").strip()
        if text:
            entries.append({"kind": "response", "title": text[:120], "full": text})
        for tc in ev.get("tool_calls") or []:
            fn      = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            call_id = str(tc.get("id") or tc.get("tool_call_id") or "")
            name    = fn.get("name", "tool")
            raw_args = fn.get("arguments", "")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except Exception:
                args = {"_raw": raw_args}

            if name in ("kanban_create", "delegate_task", "sessions_spawn"):
                assignee = args.get("assignee") or args.get("agentId") or args.get("profile") or "subagent"
                brief    = args.get("title") or args.get("task") or args.get("description") or args.get("body") or ""
                entries.append({
                    "kind":    "delegation",
                    "title":   f"→ {assignee}",
                    "detail":  str(brief)[:150],
                    "full":    json.dumps(args, indent=2, ensure_ascii=False),
                    "call_id": call_id,
                })
            elif name in ("write_file", "write"):
                path = args.get("path") or args.get("file_path") or ""
                entries.append({
                    "kind":    "file_write",
                    "title":   os.path.basename(path) if path else name,
                    "detail":  path,
                    "full":    json.dumps(args, indent=2, ensure_ascii=False),
                    "call_id": call_id,
                })
            elif name == "image_generate":
                prompt = args.get("prompt", "")
                entries.append({
                    "kind":    "tool_call",
                    "title":   f"🎨 image_generate · {str(prompt)[:80]}",
                    "detail":  str(prompt)[:160],
                    "tool":    name,
                    "full":    json.dumps(args, indent=2, ensure_ascii=False),
                    "call_id": call_id,
                })
            else:
                entries.append(_tool_call_entry(name, args, call_id))
        return entries

    if role == "tool":
        name     = ev.get("tool_name") or ev.get("name", "tool")
        content  = str(ev.get("content", "") or "")
        call_id  = str(ev.get("tool_call_id") or ev.get("id") or "")
        ctx      = _pop_tool_call_context(call_id)
        intent   = (ctx or {}).get("detail") or ""
        is_error = False
        try:
            parsed = json.loads(content) if content.startswith("{") else None
            if isinstance(parsed, dict):
                if parsed.get("error") or parsed.get("isError") or parsed.get("status") == "error":
                    is_error = True
        except Exception:
            pass
        if is_error:
            entries.append({
                "kind":    "tool_error",
                "title":   f"{name}" + (f" · {intent[:90]}" if intent else ""),
                "detail":  intent or content[:150],
                "full":    content,
                "call_id": call_id,
            })
        else:
            title = f"{name} result"
            if intent:
                title = f"{name} · {intent[:90]}"
            else:
                try:
                    parsed = json.loads(content) if content.startswith("{") else None
                    if isinstance(parsed, dict):
                        output = parsed.get("output") or parsed.get("result") or parsed.get("content")
                        if output:
                            title = f"{name} result · {str(output).replace(chr(10), ' ')[:90]}"
                except Exception:
                    pass
            entries.append({
                "kind":    "tool_result",
                "title":   title,
                "detail":  intent or content[:150],
                "full":    content,
                "call_id": call_id,
            })
        return entries

    return []
