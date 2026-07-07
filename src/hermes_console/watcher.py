from __future__ import annotations

"""
hermes_console.watcher
~~~~~~~~~~~~~~~~~~~~~~~~
Background daemon thread that continuously polls all event sources:
- Agent session JSONL files (new messages)
- state.db for non-JSONL sessions (kanban workers, cron, CLI)
- gateway.log / agent.log live tails
- Kanban board for status changes
- Delivery file directories
- Health checks (OpenRouter credits, stalled tasks, gateway state)
"""

import json
import os
import sqlite3
import threading
import time

from hermes_console.config import (
    AGENT_IDS,
    AGENT_LIVE_LOGS,
    AGENT_OUTPUT_DIRS,
    FILE_POLL_CACHE_IMAGES,
    HERMES_DIR,
    HERMES_ORCHESTRATOR,
    MEDIA_DIR,
    POLL_INTERVAL,
)
from hermes_console.events import broadcast
from hermes_console.database import _usage_dedupe_key, _insert_usage_event
from hermes_console.parsers.session import (
    _state_db_path,
    _state_row_to_event,
    latest_session_file,
    parse_hermes_event,
    _remember_tool_call,
    _credit_structured_tool_call,
    _feed_tool_recently_emitted,
    _mark_feed_tool_emitted,
)
from hermes_console.parsers.logs import parse_live_log_line
from hermes_console.services.health import (
    issues_state,
    issues_lock,
    _raise_issue,
    _resolve_issue,
)
from hermes_console.services.kanban import (
    _kanban_conn,
    kanban_board_summary,
    kanban_snapshot,
    kanban_snapshot_lock,
)
from hermes_console.services.files import (
    register_file,
    file_registry,
    file_registry_lock,
    _find_card_for_file,
    _media_agent_id,
)
from hermes_console.services.slack_trace import (
    trace,
    trace_incident,
    _trace_verbose,
    _trace_dedupe_milestone,
    _agent_emoji,
    _format_done_milestone,
    _trace_done_incidents,
    _kanban_completion_detail,
    _track_memory_preflight_from_event,
    _cache_s3_upload,
)
from hermes_console.services.prompt_trace import _trace_add
from hermes_console.services.usage import fetch_openrouter_key_status, openrouter_key_state
from hermes_console.services.files import _try_register_path

# ── Per-agent live-state ────────────────────────────────────────────────────────
# Populated by the Watcher from session events.  Read by /api/agents.
agent_state: dict[str, dict] = {
    aid: {"active": False, "last_seen": None, "session": None}
    for aid in AGENT_IDS
}

# Dedup key set for session events — prevents the ring-buffer warmup from
# double-emitting events when the watcher's live-tail catches up.
seen_events: set[tuple] = set()


class Watcher(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.session_cursors: dict[str, tuple[str, int]] = {}
        self.log_offsets:     dict[str, int]             = {}
        self.state_cursors:   dict[str, int]             = {}
        self._warmup()

    # ── Warmup ────────────────────────────────────────────────────────────────

    def _warmup(self):
        """Seed the recent-events buffer with the tail of each session."""
        self._warming_up = True
        WARMUP_TAIL              = 50
        WARMUP_FRESHNESS_SECONDS = int(os.environ.get("HERMES_WEBUI_WARMUP_FRESHNESS", "21600"))

        now       = time.time()
        all_agents = list(AGENT_IDS)

        # ── jsonl warmup ──────────────────────────────────────────────────────
        for aid in all_agents:
            f = latest_session_file(aid)
            if not f:
                continue
            try:
                size  = os.path.getsize(f)
                mtime = os.path.getmtime(f)
            except OSError:
                self.session_cursors[aid] = (f, 0)
                continue
            self.session_cursors[aid] = (f, size)
            if now - mtime > WARMUP_FRESHNESS_SECONDS:
                continue
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

        # ── log offset init ───────────────────────────────────────────────────
        for aid, paths in AGENT_LIVE_LOGS.items():
            for log_type, path in paths.items():
                source = f"{log_type}:{aid}"
                try:
                    self.log_offsets[source] = os.path.getsize(path) if os.path.exists(path) else 0
                except OSError:
                    self.log_offsets[source] = 0

        # ── state.db warmup ───────────────────────────────────────────────────
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
            for row in reversed(rows):
                if now - (row["timestamp"] or 0) > WARMUP_FRESHNESS_SECONDS:
                    continue
                ev = _state_row_to_event(row)
                self._process_session_event(aid, ev)

        self._scan_delivery_files(broadcast_event=False)
        self._warming_up = False

    # ── Main loop ──────────────────────────────────────────────────────────────

    def run(self):
        heartbeat_ts    = time.time()
        kanban_ts       = 0.0
        state_db_ts     = 0.0
        session_usage_ts = 0.0
        health_ts       = 0.0
        while True:
            try:
                self._poll_agents()
                self._poll_files()
                self._poll_logs()
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
                if now - health_ts >= 10.0:
                    self._poll_health()
                    health_ts = now
                if now - heartbeat_ts >= 15:
                    broadcast({"type": "heartbeat"})
                    heartbeat_ts = now
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)

    # ── Poll: session usage rollup from state.db ───────────────────────────────

    def _poll_session_usage(self):
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
                    ORDER BY ended_at DESC LIMIT 100
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
                    "dedupe_key":          _usage_dedupe_key("session", aid, r["id"]),
                    "ts":                  r["ended_at"] or r["started_at"] or time.time(),
                    "prompt_id":           str(r["id"]),
                    "agent":               aid,
                    "provider":            "openrouter",
                    "kind":                "session_rollup",
                    "model":               r["model"] or "",
                    "input_tokens":        r["input_tokens"] or 0,
                    "output_tokens":       r["output_tokens"] or 0,
                    "cache_read_tokens":   r["cache_read_tokens"] or 0,
                    "cache_write_tokens":  r["cache_write_tokens"] or 0,
                    "estimated_cost_usd":  r["actual_cost_usd"] if r["actual_cost_usd"] is not None else r["estimated_cost_usd"],
                    "cost_status":         r["cost_status"] or ("actual" if r["actual_cost_usd"] is not None else "estimated"),
                    "cost_source":         r["cost_source"] or "session_db",
                    "usage_units":         r["api_call_count"] or 1,
                    "session_id":          r["id"],
                    "detail":              r["title"] or r["source"] or "session",
                    "extra":               {"source": r["source"], "pricing_version": r["pricing_version"]},
                })

    # ── Poll: state.db non-jsonl messages ─────────────────────────────────────

    def _poll_state_db(self):
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

    # ── Poll: session jsonl tail ───────────────────────────────────────────────

    def _poll_agents(self):
        for aid in list(AGENT_IDS):
            f = latest_session_file(aid)
            if not f:
                continue
            try:
                size = os.path.getsize(f)
            except OSError:
                continue
            prev_file, prev_offset = self.session_cursors.get(aid, (None, 0))
            if f != prev_file:
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
            for line in new_data.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._process_session_event(aid, ev)

    # ── Session event processor ────────────────────────────────────────────────

    def _process_session_event(self, agent_id: str, ev: dict):
        role = ev.get("role", "")
        if role == "session_meta" or role not in ("user", "assistant", "tool"):
            return

        ts       = ev.get("timestamp", "")
        ts_short = ts[:19].replace("T", " ") if ts else ""

        msg_id            = ev.get("message_id") or ev.get("tool_call_id") or ""
        content_for_hash  = json.dumps(ev, sort_keys=True, default=str)[:512]
        key               = (agent_id, role, ts, msg_id, hash(content_for_hash))
        if key in seen_events:
            return
        seen_events.add(key)

        ev_session_id = ev.get("session_id") or ""
        if ev_session_id:
            sid           = ev_session_id
            session_short = sid.split("_")[-1][:8] if "_" in sid else sid[:8]
        else:
            cursor        = self.session_cursors.get(agent_id)
            session_short = None
            if cursor and cursor[0]:
                base          = os.path.basename(cursor[0])
                stem          = os.path.splitext(base)[0]
                session_short = stem.split("_")[-1][:8] if "_" in stem else stem[:8]

        agent_state[agent_id]["last_seen"] = ts_short
        agent_state[agent_id]["active"]    = True
        agent_state[agent_id]["session"]   = session_short
        broadcast({
            "type":      "agent_status",
            "agent":     agent_id,
            "active":    True,
            "last_seen": ts_short,
            "session":   session_short,
        })

        _track_memory_preflight_from_event(agent_id, ev, session_short)

        tool_name = (ev.get("tool_name") or ev.get("name", "")) if role == "tool" else ""
        if tool_name == "s3_upload":
            content = str(ev.get("content") or "")
            try:
                body = json.loads(content) if content.strip().startswith("{") else {}
                if isinstance(body, dict):
                    lp  = body.get("local_path") or ""
                    url = body.get("url") or body.get("s3_url") or ""
                    if lp and url and body.get("success"):
                        _cache_s3_upload(lp, url)
                        _try_register_path(lp, broadcast_event=True)
            except (json.JSONDecodeError, TypeError):
                pass

        if role == "assistant":
            for tc in ev.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn      = tc.get("function") or {}
                call_id = str(tc.get("id") or tc.get("tool_call_id") or "")
                name    = fn.get("name", "tool")
                raw_args = fn.get("arguments", "")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except Exception:
                    args = {}
                _remember_tool_call(agent_id, call_id, name, args)
                _credit_structured_tool_call(agent_id, name, args, ev_session_id)

        for parsed in parse_hermes_event(ev):
            kind   = parsed.get("kind")
            tool   = parsed.get("tool") or ""
            detail = (parsed.get("detail") or "").strip()
            if kind == "tool_call" and detail and _feed_tool_recently_emitted(agent_id, kind, tool, detail):
                continue
            if kind == "tool_call" and detail:
                _mark_feed_tool_emitted(agent_id, kind, tool, detail)
            broadcast({"type": "agent_event", "agent": agent_id, "ts": ts_short, **parsed})

            if kind == "tool_call":
                title      = parsed.get("title", "")
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

            if kind == "tool_error" and not getattr(self, "_warming_up", False):
                trace_incident(agent_id, "tool_error",
                               f"{parsed.get('title','?')} · {(parsed.get('detail') or '')[:120]}",
                               session_id=session_short)
            elif kind == "delegation" and _trace_verbose():
                em = _agent_emoji(agent_id)
                trace(f"{em} `{agent_id}` · delegated → `{parsed.get('title','?')}` · "
                      f"{parsed.get('detail','')[:120]}")

    # ── Poll: delivery files ───────────────────────────────────────────────────

    def _scan_delivery_files(self, *, broadcast_event: bool) -> int:
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
                    if os.path.isfile(fpath) and register_file(aid, fpath, broadcast_event=broadcast_event):
                        registered += 1
        if FILE_POLL_CACHE_IMAGES and MEDIA_DIR and os.path.isdir(MEDIA_DIR):
            try:
                names = os.listdir(MEDIA_DIR)
            except OSError:
                names = []
            for fname in names:
                if fname.startswith("."):
                    continue
                fpath = os.path.join(MEDIA_DIR, fname)
                if os.path.isfile(fpath) and register_file(_media_agent_id(), fpath, broadcast_event=broadcast_event):
                    registered += 1
        return registered

    def _poll_files(self):
        self._scan_delivery_files(broadcast_event=True)

    # ── Poll: kanban board diffs ───────────────────────────────────────────────

    def _poll_kanban(self):
        board = kanban_board_summary()
        if not board.get("available"):
            return
        new_snap = {t["id"]: t["status"] for t in board["tasks"]}
        with kanban_snapshot_lock:
            old_snap = dict(kanban_snapshot)
            kanban_snapshot.clear()
            kanban_snapshot.update(new_snap)
        if not old_snap:
            broadcast({"type": "kanban_board", **board})
            return
        changed_ids: set[str] = set()
        for tid, status in new_snap.items():
            if old_snap.get(tid) != status:
                changed_ids.add(tid)
        for tid in old_snap:
            if tid not in new_snap:
                changed_ids.add(tid)
        if changed_ids:
            for t in board["tasks"]:
                if t["id"] in changed_ids:
                    prev = old_snap.get(t["id"])
                    broadcast({"type": "kanban_card_update", "card": t, "prev_status": prev})
                    self._trace_card_transition(t, prev)
                    if t.get("status") == "done":
                        self._backfill_file_card_ids()
            broadcast({"type": "kanban_board", **board})

        seen_blocked: set[str] = set()
        for t in board.get("tasks", []):
            if t.get("status") == "blocked":
                tid      = t["id"]
                issue_id = f"task_blocked:{tid}"
                seen_blocked.add(issue_id)
                _raise_issue(issue_id, "critical", t.get("assignee") or None,
                             f"{t.get('assignee','agent')}: task BLOCKED",
                             f"Task {tid}: {(t.get('title') or '')[:80]}")
        with issues_lock:
            blocked_ids = [k for k in list(issues_state) if k.startswith("task_blocked:")]
        for k in blocked_ids:
            if k not in seen_blocked:
                _resolve_issue(k)

    def _backfill_file_card_ids(self):
        with file_registry_lock:
            unresolved = [e for e in file_registry if not e.get("card_id")]
        for entry in unresolved:
            cid = _find_card_for_file(entry["path"])
            if cid:
                entry["card_id"] = cid
                broadcast({"type": "file_event", **entry})

    def _trace_card_transition(self, card: dict, prev_status: str | None):
        tid      = card.get("id", "?")
        title    = (card.get("title") or "")[:80]
        assignee = card.get("assignee") or "?"
        status   = card.get("status", "?")
        elapsed  = card.get("elapsed_s")
        em       = _agent_emoji(assignee)

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
                trace(_format_done_milestone(card, detail))
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
            dur    = f"{elapsed}s" if elapsed is not None else "?"
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

    # ── Poll: health checks ────────────────────────────────────────────────────

    def _poll_health(self):
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
        self._check_gateway_state()
        self._check_stalled_agents()
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
        for platform, info in (gw.get("platforms") or {}).items():
            p_state = (info or {}).get("state", "")
            if p_state not in ("connected", ""):
                err = (info or {}).get("error_message") or p_state
                _raise_issue(f"platform_disconnected:{platform}", "warning", None,
                             f"{platform} platform disconnected",
                             (err[:120] if err else ""))
            else:
                _resolve_issue(f"platform_disconnected:{platform}")

    def _check_stalled_agents(self):
        STALL_THRESHOLD_S = 15 * 60
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
        now          = int(time.time())
        seen_stalled: set[str] = set()
        for tid, title, assignee, started_at in running:
            elapsed = now - (started_at or now)
            if elapsed > STALL_THRESHOLD_S:
                issue_id = f"stalled_task:{tid}"
                seen_stalled.add(issue_id)
                mins = elapsed // 60
                _raise_issue(issue_id, "warning", assignee or None,
                             f"{assignee or 'agent'}: task running {mins}m with no completion",
                             f"Task {tid}: {(title or '')[:80]}")
        with issues_lock:
            stall_ids = [k for k in list(issues_state) if k.startswith("stalled_task:")]
        for k in stall_ids:
            if k not in seen_stalled:
                _resolve_issue(k)

    _TRANSIENT_PREFIXES = ("rate_limit:", "network_error:", "model_overload:", "image_gen_fail:")
    _TRANSIENT_TTL_S    = 5 * 60

    def _expire_transient_issues(self):
        import time as _time
        now_s = _time.time()
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

    # ── Poll: live log tail ────────────────────────────────────────────────────

    def _poll_logs(self):
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

            for line in new_data.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                parsed = parse_live_log_line(source, line)
                if parsed:
                    broadcast(parsed)
                level = "INFO"
                if "[DEBUG" in line or "[DEBUG]" in line:
                    level = "DEBUG"
                elif "[WARN" in line or "[WARNING" in line:
                    level = "WARNING"
                elif "[ERROR" in line:
                    level = "ERROR"
                broadcast({"type": "log_line", "source": source, "level": level, "text": line})
