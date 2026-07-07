"""
hermes_console.services.lifecycle
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Agent gateway lifecycle — start / stop / restart via launchctl or Docker.
"""

import os
import sqlite3
import subprocess

from hermes_console.config import (
    AGENT_IDS,
    HERMES_DIR,
    HERMES_ORCHESTRATOR,
)
from hermes_console.events import broadcast

# ── launchd / Docker config ────────────────────────────────────────────────────

def _parse_agent_map(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        if not part.strip() or "=" not in part:
            continue
        key, val = part.split("=", 1)
        key = key.strip()
        val = os.path.expanduser(val.strip())
        if key and val:
            out[key] = val
    return out


_DEFAULT_AGENT_PLISTS = {
    "sage":    os.path.expanduser("~/Library/LaunchAgents/ai.hermes.gateway.plist"),
    "imagine": os.path.expanduser("~/Library/LaunchAgents/ai.hermes.gateway-imagine.plist"),
}
_DEFAULT_AGENT_LABELS = {
    "sage":    "ai.hermes.gateway",
    "imagine": "ai.hermes.gateway-imagine",
}
AGENT_PLISTS = {
    **_DEFAULT_AGENT_PLISTS,
    **_parse_agent_map(os.environ.get("HERMES_AGENT_PLISTS", "")),
}
AGENT_LAUNCHD_LABELS = {
    **_DEFAULT_AGENT_LABELS,
    **_parse_agent_map(os.environ.get("HERMES_AGENT_LABELS", "")),
}
DOCKER_CONTAINER = os.environ.get("HERMES_DOCKER_CONTAINER", "").strip()


# ── Worker PID lookup (Docker kanban workers) ──────────────────────────────────

def _get_worker_pids(agent_id: str) -> list[int]:
    db_path = os.path.join(HERMES_DIR, "kanban.db")
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path, timeout=2)
        rows = conn.execute(
            "SELECT worker_pid FROM tasks "
            "WHERE status IN ('running', 'blocked') "
            "AND assignee=? AND worker_pid IS NOT NULL",
            (agent_id,),
        ).fetchall()
        conn.close()
        return [int(r[0]) for r in rows if r[0]]
    except Exception:
        return []


def _agent_running(agent_id: str) -> bool:
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
        cp = subprocess.run(["launchctl", "list", label],
                            capture_output=True, text=True, timeout=5)
        return cp.returncode == 0
    except Exception:
        return False


# ── Lifecycle actions ──────────────────────────────────────────────────────────

def agent_lifecycle(agent_id: str, action: str) -> dict:
    """start / stop / restart an agent's gateway."""
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
        return {
            "ok": False,
            "error": f"plist not found: {plist}",
            "kind": "no_gateway",
        }
    label = AGENT_LAUNCHD_LABELS.get(agent_id, "")
    if action == "stop":
        rc, msg = _run(["launchctl", "unload", plist])
        if rc != 0:
            return {"ok": False, "message": msg}
        try:
            broadcast({"type": "agent_status", "agent": agent_id, "active": False,
                       "last_seen": None, "session": None})
        except Exception:
            pass
        return {"ok": True, "action": "stopped"}
    if action == "start":
        rc, msg = _run(["launchctl", "load", plist])
        if rc != 0:
            return {"ok": False, "message": msg}
        return {"ok": True, "action": "started"}
    # restart = stop then start
    _run(["launchctl", "unload", plist])
    rc, msg = _run(["launchctl", "load", plist])
    if rc != 0:
        return {"ok": False, "message": msg}
    return {"ok": True, "action": "restarted"}
