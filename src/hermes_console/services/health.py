from __future__ import annotations

"""
hermes_console.services.health
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Health / issue tracker.

Issues are keyed by a stable ``issue_id`` string.  They are raised
immediately when a problem is detected (credit low, stalled task, gateway
offline) and resolved as soon as the condition clears.  Every mutation is
pushed over SSE so the dashboard updates within one poll cycle (<100 ms).
"""

import threading
import time

from hermes_console.events import broadcast

# ── State ──────────────────────────────────────────────────────────────────────
issues_state: dict[str, dict] = {}
issues_lock  = threading.Lock()


def _now_hms() -> str:
    """Wall-clock timestamp string used for issue and feed entries."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _raise_issue(issue_id: str, severity: str, agent: str | None,
                 title: str, detail: str = "") -> None:
    """Register or refresh an active issue and broadcast immediately.

    severity: ``"critical"`` | ``"warning"`` | ``"info"``

    Suppresses re-broadcast if title+detail are unchanged (avoids log spam).
    """
    now = _now_hms()
    issue = {
        "id":       issue_id,
        "severity": severity,
        "agent":    agent or "",
        "title":    title,
        "detail":   detail,
        "ts":       now,
    }
    with issues_lock:
        existing = issues_state.get(issue_id, {})
        if (existing.get("title") == title
                and existing.get("detail") == detail
                and existing.get("severity") == severity):
            return  # nothing changed — skip the broadcast
        issues_state[issue_id] = issue

    broadcast({"type": "health_alert", "action": "raise", "issue": issue})


def _resolve_issue(issue_id: str) -> None:
    """Remove an issue by ID and broadcast the resolution."""
    with issues_lock:
        if issue_id not in issues_state:
            return
        issues_state.pop(issue_id, None)
    broadcast({"type": "health_alert", "action": "resolve", "issue_id": issue_id})


def _resolve_prefix(prefix: str) -> None:
    """Resolve all issues whose ID starts with *prefix*."""
    with issues_lock:
        to_remove = [k for k in issues_state if k.startswith(prefix)]
    for k in to_remove:
        _resolve_issue(k)


def _get_issues() -> list[dict]:
    """Return all active issues sorted critical-first."""
    with issues_lock:
        return sorted(
            issues_state.values(),
            key=lambda x: ("critical" != x["severity"], x["ts"]),
        )
