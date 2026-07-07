from __future__ import annotations

"""
hermes_console.services.files
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
File registry — tracks delivery files written by agents into their workspace
output dirs and optionally a shared media directory.
"""

import os
import sqlite3
import threading
import time

from hermes_console.config import (
    AGENT_IDS,
    HERMES_DIR,
    HERMES_ORCHESTRATOR,
    MEDIA_DIR,
    MEDIA_AGENT,
    WORKSPACE_DIR,
    FILE_REGISTRY_MAX_AGE_SEC,
    FILE_REGISTRY_MAX_ENTRIES,
)
from hermes_console.events import broadcast

# ── State ──────────────────────────────────────────────────────────────────────
file_registry:     list[dict]        = []
file_registry_lock = threading.Lock()
registered_paths:  dict[str, float]  = {}   # realpath → last registered mtime


# ── Helpers ────────────────────────────────────────────────────────────────────

def _media_agent_id() -> str:
    return MEDIA_AGENT if MEDIA_AGENT in AGENT_IDS else HERMES_ORCHESTRATOR


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
    if MEDIA_DIR and real.startswith(os.path.realpath(MEDIA_DIR) + os.sep):
        return _media_agent_id()
    return None


def _file_age_ok(mtime: float) -> bool:
    return (time.time() - mtime) <= FILE_REGISTRY_MAX_AGE_SEC


def _prune_file_registry_locked() -> int:
    """Drop stale entries; keep newest ``FILE_REGISTRY_MAX_ENTRIES``."""
    before = len(file_registry)
    kept   = [e for e in file_registry if _file_age_ok(e.get("mtime", 0))]
    kept.sort(key=lambda e: e.get("mtime", 0))
    if len(kept) > FILE_REGISTRY_MAX_ENTRIES:
        kept = kept[-FILE_REGISTRY_MAX_ENTRIES:]
    file_registry[:] = kept
    registered_paths.clear()
    registered_paths.update({e["path"]: e["mtime"] for e in file_registry})
    return before - len(file_registry)


def _resolve_file_path(path: str) -> str:
    """If *path* is a binary-content placeholder stub, return the actual file."""
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


def _find_card_for_file(path: str) -> str | None:
    """Look up the kanban card whose run metadata mentions *path* (best-effort)."""
    from hermes_console.services.kanban import _kanban_conn
    conn = _kanban_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT task_id FROM task_runs WHERE metadata LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{path}%",),
        )
        row = cur.fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _try_register_path(path: str, *, broadcast_event: bool = True) -> bool:
    """Register a delivery file discovered outside directory polling."""
    path = (path or "").strip()
    if not path:
        return False
    aid = _agent_id_for_path(path)
    if not aid:
        return False
    return register_file(aid, path, broadcast_event=broadcast_event)


def register_file(agent_id: str, path: str, broadcast_event: bool = True) -> bool:
    """Add or refresh a file in the registry.  Returns True if newly added or
    updated."""
    try:
        real_path = os.path.realpath(path)
    except OSError:
        return False
    try:
        stat  = os.stat(real_path)
        size  = stat.st_size
        mtime = stat.st_mtime
    except OSError:
        return False
    if not _file_age_ok(mtime):
        return False

    prev_mtime = registered_paths.get(real_path)
    is_new     = prev_mtime is None
    is_updated = prev_mtime is not None and mtime > prev_mtime + 0.5
    if not is_new and not is_updated:
        return False

    actual = _resolve_file_path(real_path)
    if actual != real_path:
        try:
            size = os.stat(actual).st_size
        except OSError:
            pass

    ext     = os.path.splitext(real_path)[1].lower()
    ts      = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
    card_id = _find_card_for_file(real_path)
    entry   = {
        "agent":    agent_id,
        "filename": os.path.basename(real_path),
        "path":     real_path,
        "size":     size,
        "mtime":    mtime,
        "ts":       ts,
        "ext":      ext,
        "card_id":  card_id,
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
        # Slack trace mirror for file writes (optional, best-effort)
        try:
            from hermes_console.services.slack_trace import trace, _trace_verbose, _agent_emoji
            if _trace_verbose():
                size_kb = max(1, size // 1024)
                em      = _agent_emoji(agent_id)
                trace(f"{em} `{agent_id}` · wrote `{entry['filename']}` ({size_kb} KB)")
        except Exception:
            pass
    return True


def _is_allowed_file_path(path: str) -> bool:
    """Security check: only serve files within agent workspaces or media dir."""
    real = os.path.realpath(path)
    if MEDIA_DIR and real.startswith(os.path.realpath(MEDIA_DIR) + os.sep):
        return True
    for aid in AGENT_IDS:
        ws = os.path.realpath(os.path.join(WORKSPACE_DIR, aid))
        if real.startswith(ws + os.sep):
            return True
    return False
