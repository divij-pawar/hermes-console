"""
hermes_console.services.backup
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Git backup helpers — status check and run.
"""

import os
import subprocess

from hermes_console.config import BACKUP_REPO, BACKUP_SCRIPT


def read_last_lines(path: str, n: int) -> list[str]:
    """Read the last *n* lines of *path* without loading the whole file."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size  = f.tell()
            chunk = min(size, n * 200)
            f.seek(max(0, size - chunk))
            data  = f.read()
        return data.decode("utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []


def backup_status() -> dict:
    configured = bool(BACKUP_REPO and BACKUP_SCRIPT and os.path.isfile(BACKUP_SCRIPT))
    base = {
        "configured": configured,
        "repo":       BACKUP_REPO or None,
        "script":     BACKUP_SCRIPT or None,
    }
    if not configured:
        return {
            **base, "ok": False,
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
                "hash":    parts[0] if len(parts) > 0 else "",
                "message": parts[1] if len(parts) > 1 else "",
                "date":    parts[2][:16] if len(parts) > 2 else "",
            }
        cp2   = subprocess.run(
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
            "ok": False, "configured": False,
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
        output       = (cp.stdout + cp.stderr).strip()
        committed    = "Committed:" in output
        commit_hash  = None
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
            "ok":          cp.returncode == 0,
            "output":      output,
            "configured":  True,
            "repo":        BACKUP_REPO,
            "script":      BACKUP_SCRIPT,
            "committed":   committed,
            "commit_hash": commit_hash,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "configured": True, "error": "Backup timed out (180s)"}
    except Exception as e:
        return {"ok": False, "configured": True, "error": str(e)}
