"""
hermes_console.routes.handler
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
HTTP request handler and threaded server.  All GET and POST routes live here.
SSE connection lifecycle is managed in ``_sse()``.
"""

import http.server
import json
import os
import queue
import socketserver
import sqlite3
import subprocess
import threading
import urllib.parse

_CLIENT_GONE = (BrokenPipeError, ConnectionResetError)

from hermes_console.config import (
    AGENT_IDS,
    AGENT_LIVE_LOGS,
    BACKUP_REPO,
    BACKUP_SCRIPT,
    GATEWAY_LOG,
    HERMES_DIR,
    HERMES_EXTRA_HOME,
    HERMES_ORCHESTRATOR,
    HERMES_WEBUI_HOST,
    LOGS_DIR,
    MEDIA_AGENT,
    MEDIA_DIR,
    MONITOR_DB,
    PORT,
    PROFILES_DIR,
    SCRIPT_DIR,
    SLACK_TRACE_CHANNEL,
    STATIC_DIR,
    TRACE_ENABLED,
    TRACE_MODE,
    WORKSPACE_DIR,
    _AGENT_HOME_MAP,
)
from hermes_console.events import (
    sse_clients,
    sse_lock,
    recent_events,
    recent_events_lock,
)
from hermes_console.services.activity import activity_snapshot
from hermes_console.services.backup import backup_status, run_backup, read_last_lines
from hermes_console.services.files import (
    file_registry,
    file_registry_lock,
    _is_allowed_file_path,
    _resolve_file_path,
)
from hermes_console.services.health import _get_issues
from hermes_console.services.kanban import (
    kanban_board_summary,
    kanban_card_detail,
    kanban_archive_one,
    kanban_archive_done,
    kanban_cancel_one,
)
from hermes_console.services.lifecycle import (
    AGENT_PLISTS,
    DOCKER_CONTAINER,
    _agent_running,
    _get_worker_pids,
    agent_lifecycle,
)
from hermes_console.services.prompt_trace import prompt_trace_snapshot
from hermes_console.services.usage import _agent_metadata_list, usage_snapshot
from hermes_console.watcher import agent_state

MIME: dict[str, str] = {
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

    # ── GET ────────────────────────────────────────────────────────────────────

    def do_GET(self):  # noqa: C901  (many routes — complexity expected)
        path = self.path.split("?")[0].rstrip("/") or "/"
        qs   = self.path[len(path):]

        if path in ("", "/"):
            self._serve_file(os.path.join(STATIC_DIR, "index.html"))

        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            self._serve_file(os.path.join(STATIC_DIR, rel))

        elif path == "/api/events":
            self._sse()

        elif path == "/api/agents":
            self._send_json({
                "orchestrator": HERMES_ORCHESTRATOR,
                "agents":       _agent_metadata_list(),
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

        elif path == "/api/console-log":
            self._console_log(qs)

        elif path == "/api/prompt-traces":
            self._send_json(prompt_trace_snapshot())

        elif path == "/api/health":
            self._send_json({"issues": _get_issues()})

        elif path == "/api/info":
            self._info()

        elif path == "/api/cron":
            self._cron_list()

        elif path == "/api/settings":
            self._settings_get()

        else:
            self._send_json({"error": "not found"}, 404)

    # ── POST ───────────────────────────────────────────────────────────────────

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
            rest = path[len("/api/agent/"):].strip("/").split("/")
            if len(rest) == 2:
                agent_id, action = rest
                self._send_json(agent_lifecycle(agent_id, action))
            else:
                self._send_json({"error": "expected /api/agent/<id>/<action>"}, 400)

        elif path.startswith("/api/cron/"):
            self._cron_action(path)

        elif path == "/api/settings":
            self._settings_save()

        else:
            self._send_json({"error": "not found"}, 404)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _serve_file(self, fpath: str):
        if not os.path.isfile(fpath):
            self._send_json({"error": "not found"}, 404)
            return
        ext  = os.path.splitext(fpath)[1]
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
        except _CLIENT_GONE:
            return
        except OSError:
            self._send_json({"error": "read error"}, 500)

    def _send_json(self, data: dict, code: int = 200):
        try:
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_GONE:
            return

    def _status(self):
        agents_out = {}
        for aid in AGENT_IDS:
            s = agent_state.get(aid, {})
            agents_out[aid] = {
                "active":          s.get("active", False),
                "last_seen":       s.get("last_seen"),
                "session":         s.get("session"),
                "gateway_running": _agent_running(aid),
                "has_gateway":     (aid == HERMES_ORCHESTRATOR) if DOCKER_CONTAINER else (aid in AGENT_PLISTS),
                "pids":            (_get_worker_pids(aid) if aid != HERMES_ORCHESTRATOR else []),
                "worker_count":    (len(_get_worker_pids(aid)) if aid != HERMES_ORCHESTRATOR else 0),
            }
        board = kanban_board_summary()
        self._send_json({
            "agents": agents_out,
            "kanban": {"counts": board.get("counts", {}), "available": board.get("available", False)},
        })

    def _files(self):
        with file_registry_lock:
            entries = list(reversed(file_registry))
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

    def _console_log(self, qs: str):
        params = {}
        if qs.startswith("?"):
            for part in qs[1:].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
        try:
            lines = int(params.get("lines", 200))
        except ValueError:
            lines = 200
        lines = max(1, min(lines, 500))
        log_path = os.path.join(SCRIPT_DIR, "console.log")
        if not os.path.isfile(log_path):
            self._send_json({"lines": [], "path": log_path, "error": "console.log not found"})
            return
        self._send_json({"lines": read_last_lines(log_path, lines), "path": log_path})

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
        path = _resolve_file_path(path)
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        ext  = os.path.splitext(path)[1].lower()
        mime = MIME.get(ext, "text/plain; charset=utf-8")
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_cors()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'inline; filename="{os.path.basename(path)}"')
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
        source   = params.get("source", "gateway")
        try:
            n = int(params.get("lines", 200))
        except ValueError:
            n = 200
        path_map = {aid: AGENT_LIVE_LOGS[aid].get("gateway", AGENT_LIVE_LOGS[aid]["agent"])
                    for aid in AGENT_LIVE_LOGS}
        path_map.setdefault("gateway", GATEWAY_LOG)
        path  = path_map.get(source, GATEWAY_LOG)
        lines = read_last_lines(path, n)
        self._send_json({"lines": lines, "source": source})

    def _info(self):
        def _get_hermes_version() -> str:
            try:
                cp = subprocess.run(["hermes", "version"],
                                    capture_output=True, text=True, timeout=5)
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
            "app":                "Hermes Console",
            "hermes_dir":         HERMES_DIR,
            "orchestrator":       HERMES_ORCHESTRATOR,
            "docker_container":   DOCKER_CONTAINER or None,
            "hermes_version":     _get_hermes_version(),
            "gateway_pid":        _get_gateway_pid(),
            "active_worker_pids": worker_pids,
            "monitor_db":         MONITOR_DB,
            "webui_host":         HERMES_WEBUI_HOST,
            "webui_port":         int(os.environ.get("HERMES_WEBUI_PORT", 7979)),
        })

    def _cron_list(self):
        def _read_cron_db() -> list:
            db_path = os.path.join(HERMES_DIR, "cron.db")
            if not os.path.exists(db_path):
                return []
            try:
                conn   = sqlite3.connect(db_path, timeout=2)
                conn.row_factory = sqlite3.Row
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()]
                tbl = next(
                    (t for t in tables if "cron" in t.lower() or "job" in t.lower()),
                    tables[0] if tables else None,
                )
                if not tbl:
                    conn.close()
                    return []
                rows   = conn.execute(f"SELECT * FROM {tbl} ORDER BY rowid DESC").fetchall()
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
            if cp.returncode == 0 and cp.stdout.strip().startswith("["):
                self._send_json({"jobs": json.loads(cp.stdout)})
            elif cp.returncode == 0 and cp.stdout.strip().startswith("{"):
                parsed = json.loads(cp.stdout)
                self._send_json({"jobs": parsed.get("jobs", parsed.get("data", []))})
            else:
                raise RuntimeError("cli output not json")
        except Exception:
            self._send_json({"jobs": _read_cron_db()})

    def _cron_action(self, path: str):
        parts = path.strip("/").split("/")
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
                    self._send_json({
                        "ok":     cp.returncode == 0,
                        "action": cron_action,
                        "id":     cron_id,
                        "msg":    (cp.stderr or cp.stdout).strip(),
                    })
                except Exception as exc:
                    self._send_json({"ok": False, "error": str(exc)}, 500)
        else:
            self._send_json({"error": "expected /api/cron/<id>/<action>"}, 400)

    def _settings_get(self):
        self._send_json({
            "hermes_dir":          HERMES_DIR,
            "orchestrator":        HERMES_ORCHESTRATOR,
            "docker_container":    DOCKER_CONTAINER,
            "webui_host":          HERMES_WEBUI_HOST,
            "webui_port":          PORT,
            "extra_home":          HERMES_EXTRA_HOME,
            "agent_plists":        os.environ.get("HERMES_AGENT_PLISTS", ""),
            "agent_labels":        os.environ.get("HERMES_AGENT_LABELS", ""),
            "trace_mode":          TRACE_MODE,
            "slack_trace_channel": SLACK_TRACE_CHANNEL,
            "warmup_freshness":    int(os.environ.get("HERMES_WEBUI_WARMUP_FRESHNESS", 21600)),
            "media_dir":           MEDIA_DIR,
            "media_agent":         MEDIA_AGENT,
            "backup_repo":         BACKUP_REPO or "",
            "backup_script":       BACKUP_SCRIPT or "",
            "vector_db_url":       os.environ.get("HERMES_LOG_DB_URL", ""),
            "files_max_age_days":  int(os.environ.get("HERMES_FILES_MAX_AGE_DAYS", 14)),
            "files_max_entries":   int(os.environ.get("HERMES_FILES_MAX_ENTRIES", 80)),
            "trace_enabled":       TRACE_ENABLED,
            "backup_configured":   bool(BACKUP_REPO and BACKUP_SCRIPT and os.path.isfile(BACKUP_SCRIPT)),
        })

    def _settings_save(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        data = json.loads(body)
        env_local = os.path.join(SCRIPT_DIR, ".env.local")
        KEY_MAP = {
            "hermes_dir":          "HERMES_DIR",
            "orchestrator":        "HERMES_ORCHESTRATOR",
            "docker_container":    "HERMES_DOCKER_CONTAINER",
            "webui_host":          "HERMES_WEBUI_HOST",
            "webui_port":          "HERMES_WEBUI_PORT",
            "extra_home":          "HERMES_EXTRA_HOME",
            "agent_plists":        "HERMES_AGENT_PLISTS",
            "agent_labels":        "HERMES_AGENT_LABELS",
            "trace_mode":          "HERMES_TRACE_MODE",
            "slack_trace_channel": "SLACK_TRACE_CHANNEL",
            "warmup_freshness":    "HERMES_WEBUI_WARMUP_FRESHNESS",
            "media_dir":           "HERMES_MEDIA_DIR",
            "media_agent":         "HERMES_MEDIA_AGENT",
            "backup_repo":         "HERMES_BACKUP_REPO",
            "backup_script":       "HERMES_BACKUP_SCRIPT",
            "vector_db_url":       "HERMES_LOG_DB_URL",
            "files_max_age_days":  "HERMES_FILES_MAX_AGE_DAYS",
            "files_max_entries":   "HERMES_FILES_MAX_ENTRIES",
        }
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

    # ── SSE ────────────────────────────────────────────────────────────────────

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

        # Initial snapshot burst
        try:
            issues_init = json.dumps({"type": "health_init", "issues": _get_issues()})
            self.wfile.write(f"data: {issues_init}\n\n".encode())
            board = kanban_board_summary()
            self.wfile.write(f"data: {json.dumps({'type': 'kanban_board', **board})}\n\n".encode())
            self.wfile.write(f"data: {json.dumps({'type': 'usage_update', **usage_snapshot()})}\n\n".encode())
            self.wfile.write(f"data: {json.dumps({'type': 'prompt_trace_init', **prompt_trace_snapshot()})}\n\n".encode())
            for aid in AGENT_IDS:
                s   = agent_state.get(aid, {})
                msg = json.dumps({
                    "type":      "agent_status",
                    "agent":     aid,
                    "active":    s.get("active", False),
                    "last_seen": s.get("last_seen"),
                    "session":   s.get("session"),
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

        # Streaming loop
        try:
            while True:
                try:
                    payload = q.get(timeout=20)
                    self.wfile.write(payload.encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except Exception:
            pass
        finally:
            with sse_lock:
                if q in sse_clients:
                    sse_clients.remove(q)


# ── Server ─────────────────────────────────────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads      = True
    allow_reuse_address = True


def main():
    from hermes_console.services.slack_trace import _trace_worker, trace
    from hermes_console.watcher import Watcher

    if TRACE_ENABLED:
        threading.Thread(target=_trace_worker, daemon=True, name="trace-worker").start()
        trace("🟢 hermes-monitor · trace mirror online")

    watcher = Watcher()
    watcher.start()

    server       = ThreadedHTTPServer((HERMES_WEBUI_HOST, PORT), Handler)
    display_host = "localhost" if HERMES_WEBUI_HOST in ("127.0.0.1", "0.0.0.0") else HERMES_WEBUI_HOST
    print(f"Hermes Console running at http://{display_host}:{PORT}")
    print(f"  Hermes home : {HERMES_DIR}")
    if HERMES_EXTRA_HOME:
        print(f"  Extra home  : {HERMES_EXTRA_HOME}  (Docker lab)")
    print(f"  Profiles    : {PROFILES_DIR}")
    print(f"  Workspaces  : {WORKSPACE_DIR}")
    print(f"  Logs dir    : {LOGS_DIR}")
    print(f"  Static dir  : {STATIC_DIR}")
    print(f"  Bind host   : {HERMES_WEBUI_HOST}")
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
