from __future__ import annotations

"""
hermes_console.services.kanban
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Kanban DB access — read-only queries for the board/card endpoints plus the
write helpers used by the cancel/archive actions.

kanban.db is owned by the Hermes gateway process.  We open it read-only
wherever possible so our reads never block gateway writes.
"""

import json
import os
import sqlite3
import subprocess
import threading
import time

from hermes_console.config import HERMES_DIR, KANBAN_DB, KANBAN_LOGS
from hermes_console.events import broadcast

# ── Board snapshot (delta tracking) ───────────────────────────────────────────
kanban_snapshot:      dict = {}   # task_id → last-seen status string
kanban_snapshot_lock = threading.Lock()


# ── Connections ────────────────────────────────────────────────────────────────

def _kanban_conn():
    try:
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


# ── Hermes CLI wrapper ─────────────────────────────────────────────────────────

def _hermes_kanban_run(*args: str) -> tuple[bool, str]:
    """Run a ``hermes kanban`` subcommand.  Returns (success, combined output)."""
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


# ── Direct DB mutations (fallback when CLI cannot) ─────────────────────────────

def _kanban_archive_direct(task_id: str, prev_status: str | None = None) -> dict:
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
            (task_id, "archived",
             json.dumps({"source": "hermes-monitor", "prev_status": prev_status}), now),
        )
        conn.commit()
        return {"ok": True, "archived": task_id, "method": "direct"}
    except sqlite3.Error as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()


def _kanban_reclaim_direct(task_id: str) -> bool:
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
        from hermes_console.services.health import _resolve_prefix
        _resolve_prefix(f"task_blocked:{task_id}")
        _resolve_prefix(f"stalled_task:{task_id}")
        board = kanban_board_summary()
        broadcast({"type": "kanban_board", **board})
    except Exception:
        pass


# ── Board queries ───────────────────────────────────────────────────────────────

def _row_to_card(row) -> dict:
    tid, title, assignee, status, created_at, started_at, completed_at = row
    elapsed = None
    if started_at:
        end = completed_at or int(time.time())
        elapsed = max(0, end - started_at)
    return {
        "id":           tid,
        "title":        title,
        "assignee":     assignee or "",
        "status":       status,
        "created_at":   created_at,
        "started_at":   started_at,
        "completed_at": completed_at,
        "elapsed_s":    elapsed,
    }


def kanban_board_summary(limit_done: int = 30) -> dict:
    conn = _kanban_conn()
    if not conn:
        return {"available": False, "tasks": [], "counts": {}}
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, title, assignee, status, created_at, started_at, completed_at "
            "FROM tasks WHERE status NOT IN ('done', 'archived') ORDER BY created_at DESC"
        )
        active = [_row_to_card(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT id, title, assignee, status, created_at, started_at, completed_at "
            "FROM tasks WHERE status = 'done' ORDER BY completed_at DESC LIMIT ?",
            (limit_done,),
        )
        done = [_row_to_card(r) for r in cur.fetchall()]
        cur.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        counts = {row[0]: row[1] for row in cur.fetchall()}
        return {"available": True, "tasks": active + done, "counts": counts}
    except sqlite3.Error as e:
        return {"available": False, "error": str(e), "tasks": [], "counts": {}}
    finally:
        conn.close()


def kanban_card_detail(task_id: str) -> dict:
    from hermes_console.services.backup import read_last_lines  # avoid circular
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
            ["id", "title", "body", "assignee", "status", "priority", "created_by",
             "created_at", "started_at", "completed_at", "workspace_kind",
             "workspace_path", "tenant", "result", "last_failure_error"], row,
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

        comments = []
        try:
            cur.execute(
                "SELECT id, author, body, created_at "
                "FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            )
            for c in cur.fetchall():
                comments.append({"id": c[0], "author": c[1], "body": c[2], "created_at": c[3]})
        except sqlite3.Error:
            pass

        parents, children = [], []
        try:
            cur.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,))
            parents = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (task_id,))
            children = [r[0] for r in cur.fetchall()]
        except sqlite3.Error:
            pass

        log_path = os.path.join(KANBAN_LOGS, f"{task_id}.log")
        log_tail = read_last_lines(log_path, 80) if os.path.isfile(log_path) else []

        return {
            "task": task, "events": events, "runs": runs,
            "comments": comments, "parents": parents, "children": children,
            "log_tail": log_tail,
        }
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        conn.close()


def kanban_cancel_one(task_id: str) -> dict:
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
    return {"ok": False, "error": direct.get("error") or msg or "archive failed", "steps": steps}


def kanban_archive_one(task_id: str) -> dict:
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
    return {"ok": False, "error": direct.get("error") or msg or "archive failed"}


def kanban_archive_done() -> dict:
    try:
        conn = _kanban_conn()
    except Exception as exc:
        return {"ok": False, "error": f"kanban.db unavailable: {exc}"}
    try:
        cur  = conn.execute("SELECT id FROM tasks WHERE status='done' ORDER BY completed_at ASC")
        ids  = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    archived: list[str] = []
    failed:   list[dict] = []
    for tid in ids:
        result = kanban_archive_one(tid)
        if result.get("ok"):
            archived.append(tid)
        else:
            failed.append({"id": tid, "error": (result.get("error") or "unknown")[:120]})
    try:
        with kanban_snapshot_lock:
            for tid in archived:
                kanban_snapshot.pop(tid, None)
        board = kanban_board_summary()
        broadcast({"type": "kanban_board", **board})
    except Exception:
        pass
    return {"ok": True, "archived": archived, "failed": failed, "count": len(archived)}
