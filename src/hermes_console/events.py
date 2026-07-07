"""
hermes_console.events
~~~~~~~~~~~~~~~~~~~~~
In-process event bus.  `broadcast()` is the single write point that fans out
to all connected SSE clients and optionally appends to the ring buffer.

Import this module early — everything else that calls broadcast() depends on it.
"""

import collections
import json
import queue
import threading

# ── SSE fan-out ────────────────────────────────────────────────────────────────
sse_clients: list[queue.Queue] = []
sse_lock = threading.Lock()

# Ring buffer of the last N broadcast events.  Replayed to new SSE clients so
# the dashboard reflects current state instead of a blank slate.  Heartbeats
# and log_line entries are excluded — they're high-frequency and not useful as
# backlog.
RECENT_EVENT_LIMIT = 200
recent_events: collections.deque = collections.deque(maxlen=RECENT_EVENT_LIMIT)
recent_events_lock = threading.Lock()

# Event types that are worth keeping in the replay backlog.
_BACKLOG_TYPES = frozenset({"agent_event", "agent_status", "file_event"})


def broadcast(event: dict) -> None:
    """Serialise *event* as an SSE data frame and push it to every connected
    client.  Dead clients (full queue) are culled in-place.

    Events whose ``type`` is in ``_BACKLOG_TYPES`` are also appended to the
    ring buffer so newly-connected clients receive recent context.
    """
    payload = f"data: {json.dumps(event)}\n\n"

    if event.get("type") in _BACKLOG_TYPES:
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
