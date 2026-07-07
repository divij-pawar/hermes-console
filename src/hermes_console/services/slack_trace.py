from __future__ import annotations

"""
hermes_console.services.slack_trace
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Slack mirror for system-side events — dispatch delays, kanban transitions,
tool errors, memory searches.  Silently disabled when SLACK_BOT_TOKEN or
SLACK_TRACE_CHANNEL are not set.

Only the trace() and trace_incident() functions are called from outside this
module.  All the milestone builders are internal.
"""

import json
import os
import queue
import re
import sqlite3
import sys
import time
import urllib.request

from hermes_console.config import (
    HERMES_DIR,
    HERMES_ORCHESTRATOR,
    SLACK_TRACE_TOKEN,
    SLACK_TRACE_CHANNEL,
    TRACE_ENABLED,
    TRACE_MODE,
    _KNOWN_AGENT_META,
)

# ── State ──────────────────────────────────────────────────────────────────────
trace_queue:             queue.Queue           = queue.Queue(maxsize=500)
_trace_milestone_seen:   set[tuple[str, str]]  = set()
_trace_incident_seen:    dict[str, float]      = {}
_memory_search_pending:  dict[str, str]        = {}   # call_id → query label
_s3_url_by_local_path:   dict[str, str]        = {}   # realpath → presigned URL
_INCIDENT_DEDUPE_TTL_S = 300

# Regexes used in memory-search milestone parsing
_MEMORY_SEARCH_CMD = re.compile(r"memory\.py\s+search\s+[\"'](.+?)[\"']", re.IGNORECASE | re.DOTALL)
_MEMORY_TYPE_FLAG  = re.compile(r"--type\s+(\w+)", re.IGNORECASE)
_MEMORY_HITS_LINE  = re.compile(r"(\d+)\s+memories returned", re.IGNORECASE)
_MEMORY_TOP_MATCH  = re.compile(r"\[\d+\]\s+(\d+)%\s+match", re.IGNORECASE)


# ── Low-level post ─────────────────────────────────────────────────────────────

def _post_to_slack(text: str) -> None:
    """POST a single chat.postMessage.  Stdlib only."""
    if not TRACE_ENABLED:
        return
    body = json.dumps({
        "channel": SLACK_TRACE_CHANNEL,
        "text":    text[:3500],
    }).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=body,
        headers={
            "Authorization": f"Bearer {SLACK_TRACE_TOKEN}",
            "Content-Type":  "application/json; charset=utf-8",
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
    """Worker thread: pull from trace_queue, throttle to ~1 msg/sec, POST."""
    while True:
        text = trace_queue.get()
        if text is None:
            return
        _post_to_slack(text)
        time.sleep(1.0)


def trace(text: str) -> None:
    """Enqueue a one-line trace message.  No-op if mirroring is disabled."""
    if not TRACE_ENABLED:
        return
    try:
        trace_queue.put_nowait(text)
    except queue.Full:
        pass


def _trace_verbose() -> bool:
    return TRACE_MODE == "verbose"


def _agent_emoji(agent_id: str) -> str:
    meta = _KNOWN_AGENT_META.get(agent_id)
    return meta["emoji"] if meta else "•"


# ── Deduplication helpers ──────────────────────────────────────────────────────

def _trace_dedupe_milestone(key: tuple[str, str]) -> bool:
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


# ── Public API ─────────────────────────────────────────────────────────────────

def trace_incident(agent_id: str, kind: str, detail: str, *,
                   severity: str = "error", session_id: str | None = None) -> None:
    """Always-on incident posts — not suppressed in milestone mode."""
    if not TRACE_ENABLED:
        return
    prefix      = {"error": "🚨", "warn": "⚠️", "blocked": "⛔"}.get(severity, "🚨")
    sess        = f" · session `{_short_session(session_id)}`" if session_id else ""
    dedupe_key  = f"{agent_id}:{kind}:{detail[:80]}"
    now         = time.time()
    if now - _trace_incident_seen.get(dedupe_key, 0) < _INCIDENT_DEDUPE_TTL_S:
        return
    _trace_incident_seen[dedupe_key] = now
    trace(f"{prefix} `{agent_id}` · {kind} · {detail[:200]}{sess}")


# ── S3 URL cache ───────────────────────────────────────────────────────────────

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
    cached = _lookup_s3_url(local_path)
    if cached:
        return cached
    try:
        if not os.path.isfile(local_path):
            return None
        if HERMES_DIR not in sys.path:
            sys.path.insert(0, HERMES_DIR)
        from s3_store import is_available, upload_workspace_file  # type: ignore
        if not is_available():
            return None
        url = upload_workspace_file(local_path)
        _cache_s3_upload(local_path, url)
        return url
    except Exception:
        return None


# ── Milestone helpers ──────────────────────────────────────────────────────────

def _session_from_log_line(line: str) -> str:
    m = re.search(r"\[(?P<session>[^\]]+)\]", line)
    return m.group("session") if m else ""


def _sanitize_trace_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip()
    for marker in ("<parameter", "<tool_call", "<function"):
        idx = cleaned.find(marker)
        if idx > 0:
            cleaned = cleaned[:idx].rstrip('", \n')

    def _path_sub(m: re.Match) -> str:
        path   = m.group(0)
        cached = _lookup_s3_url(path)
        return cached if cached else os.path.basename(path.rstrip("/"))

    cleaned = re.sub(r"/Users[^\s\"'`<>]+", _path_sub, cleaned)
    cleaned = re.sub(r"~/.hermes[^\s\"'`<>]+", _path_sub, cleaned)
    return cleaned.strip()


def _resolve_output_link(meta: dict) -> str | None:
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


def _kanban_completion_detail(task_id: str) -> dict | None:
    from hermes_console.services.kanban import _kanban_conn
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
        return {"summary": row[0] or "", "metadata": meta, "error": row[2] or "", "status": row[3] or ""}
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _format_done_milestone(card: dict, detail: dict | None) -> str:
    tid      = card.get("id", "?")
    assignee = card.get("assignee") or "?"
    title    = (card.get("title") or "")[:80]
    em       = _agent_emoji(assignee)
    head     = f"✅ {em} `{assignee}` · `{tid}` done · _{title}_"
    lines    = [head]
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
            chips.append("layers: " + (", ".join(str(x) for x in layers) if isinstance(layers, list) else str(layers)))
        kf = meta.get("key_findings")
        if isinstance(kf, list):
            for bullet in kf[:2]:
                lines.append(f"• {_sanitize_trace_text(str(bullet))[:120]}")
        gaps = meta.get("gaps_flagged")
        if gaps:
            chips.append(f"gaps: {gaps if isinstance(gaps, str) else str(len(gaps))}")
        out_link = _resolve_output_link(meta)
        out_path = meta.get("output_path") or meta.get("image_path")
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
    meta     = detail.get("metadata") or {}
    assignee = card.get("assignee") or "?"
    tid      = card.get("id", "?")
    if meta.get("degraded"):
        trace_incident(assignee, "done degraded", f"degraded completion · `{tid}`", severity="warn")
    needs = meta.get("needs_signal")
    if needs:
        trace_incident(assignee, "gaps", f"needs_signal: {str(needs)[:100]} · `{tid}`", severity="warn")
    gaps = meta.get("gaps_flagged")
    if gaps:
        gdesc = gaps if isinstance(gaps, str) else f"{len(gaps)} items"
        trace_incident(assignee, "gaps", f"gaps_flagged: {gdesc} · `{tid}`", severity="warn")


def _trace_milestone_inbound(agent_id: str, msg: str, platform: str, chat: str) -> None:
    if _trace_verbose():
        return
    em      = _agent_emoji(agent_id)
    snippet = (msg or "").replace("\n", " ")[:120]
    chan    = chat if chat else platform or "?"
    trace(f"{em} `{agent_id}` · user in `{chan}` · \"{snippet}\"")


def _trace_milestone_delivered(agent_id: str, elapsed_s: float,
                               api_calls: int, response_chars: int) -> None:
    if _trace_verbose():
        return
    em = _agent_emoji(agent_id)
    trace(
        f"{em} `{agent_id}` · delivered to front · {response_chars:,} chars · "
        f"{api_calls} API calls · {elapsed_s:.1f}s"
    )


def _memory_search_label(command: str) -> str:
    cmd   = (command or "").replace("\n", " ").strip()
    m     = _MEMORY_SEARCH_CMD.search(cmd)
    query = m.group(1).strip() if m else "?"
    if len(query) > 70:
        query = query[:69] + "…"
    type_m = _MEMORY_TYPE_FLAG.search(cmd)
    type_tag = f" · {type_m.group(1)}" if type_m else ""
    return f"{query}{type_tag}"


def _parse_memory_search_output(content: str) -> tuple[int | None, int | None]:
    output = content
    try:
        parsed = json.loads(content) if content.strip().startswith("{") else None
        if isinstance(parsed, dict):
            output = str(parsed.get("output") or parsed.get("result") or content)
    except (json.JSONDecodeError, TypeError):
        pass
    hits_m  = _MEMORY_HITS_LINE.search(output)
    hits    = int(hits_m.group(1)) if hits_m else None
    top_m   = _MEMORY_TOP_MATCH.search(output)
    top_pct = int(top_m.group(1)) if top_m else None
    return hits, top_pct


def _trace_milestone_memory_search(agent_id: str, query_label: str,
                                   hits: int | None, top_pct: int | None,
                                   session_id: str | None) -> None:
    if _trace_verbose():
        return
    dedupe_key = (session_id or "?", f"mem:{query_label[:80]}")
    if _trace_dedupe_milestone(dedupe_key):
        return
    em    = _agent_emoji(agent_id)
    hit_s = f"{hits} hit(s)" if hits is not None else "done"
    top_s = f" · top {top_pct}%" if top_pct is not None else ""
    trace(f"{em} `{agent_id}` · preflight · memory.search · \"{query_label}\" · {hit_s}{top_s}")


def _track_memory_preflight_from_event(agent_id: str, ev: dict,
                                       session_short: str | None) -> None:
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
    call_id     = str(ev.get("tool_call_id") or ev.get("id") or "")
    query_label = _memory_search_pending.pop(call_id, None)
    if not query_label:
        return
    content  = str(ev.get("content") or "")
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
        trace_incident(agent_id, "memory search failed", f"\"{query_label[:80]}\"",
                       session_id=session_short, severity="warn")
        return
    hits, top_pct = _parse_memory_search_output(content)
    _trace_milestone_memory_search(agent_id, query_label, hits, top_pct, session_short)
