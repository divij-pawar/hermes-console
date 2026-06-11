#!/usr/bin/env python3
"""
Live trajectory watcher for Hermes agents.

Usage:
    python3 watch_agents.py              # watch all configured agents
    python3 watch_agents.py sage         # watch sage only
    python3 watch_agents.py imagine      # watch imagine only

Tails each agent's latest session jsonl and prints assistant turns, tool
calls, and tool results to the terminal in colour. Sage runs in the default
profile (~/.hermes/sessions); named profiles namespace under
~/.hermes/profiles/<name>/sessions.
"""

import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

HERMES_DIR = os.path.expanduser("~/.hermes")
PROFILES_DIR = os.path.join(HERMES_DIR, "profiles")

AGENTS = {
    "sage": {
        "label": "SAGE 🧭",
        "color": "\033[94m",   # blue
    },
    "imagine": {
        "label": "IMAGINE 🎨",
        "color": "\033[95m",   # magenta
    },
}

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"

SEEN_EVENTS = set()


def _agent_root(agent_id: str) -> str:
    if agent_id == "sage":
        return HERMES_DIR
    return os.path.join(PROFILES_DIR, agent_id)


def format_ts(ts_str: str) -> str:
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%H:%M:%S")
    except Exception:
        return ts_str[:8]


def latest_session_file(agent_id: str) -> str | None:
    pattern = os.path.join(_agent_root(agent_id), "sessions", "*.jsonl")
    files = [
        f for f in glob.glob(pattern)
        if ".bak" not in f and ".reset" not in f and "trajectory" not in f
    ]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def render_content_item(item: dict) -> str | None:
    t = item.get("type", "")
    if t == "text":
        text = item.get("text", "").strip()
        if not text:
            return None
        if len(text) > 600:
            text = text[:600] + f"  {DIM}[...truncated]{RESET}"
        return text
    if t == "toolCall":
        name = item.get("name", "?")
        args = item.get("arguments", {})
        args_str = json.dumps(args, ensure_ascii=False)
        if len(args_str) > 400:
            args_str = args_str[:400] + "..."
        return f"{YELLOW}TOOL CALL{RESET}  {BOLD}{name}{RESET}\n         {DIM}{args_str}{RESET}"
    if t == "toolResult":
        name = item.get("toolName", "?")
        content = item.get("content", [])
        result_text = ""
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    result_text = c.get("text", "")
                    break
        elif isinstance(content, str):
            result_text = content
        if len(result_text) > 300:
            result_text = result_text[:300] + "..."
        is_err = item.get("isError", False)
        color = RED if is_err else DIM
        label = "TOOL ERROR" if is_err else "TOOL RESULT"
        return f"{color}{label}  {name}\n         {result_text}{RESET}"
    return None


def process_event(agent_id: str, event: dict) -> None:
    cfg = AGENTS[agent_id]
    color = cfg["color"]
    label = cfg["label"]

    t = event.get("type", "")
    uid = event.get("id") or event.get("ts") or str(event)

    global SEEN_EVENTS
    key = f"{agent_id}:{uid}"
    if key in SEEN_EVENTS:
        return
    SEEN_EVENTS.add(key)

    if t == "message":
        msg = event.get("message", {})
        role = msg.get("role", "")
        content = msg.get("content", [])
        ts = event.get("timestamp", "")
        time_str = format_ts(ts) if ts else ""

        if not isinstance(content, list):
            return

        lines = []
        for item in content:
            rendered = render_content_item(item)
            if rendered:
                lines.append(rendered)

        if not lines:
            return

        if role == "user":
            prefix = f"{DIM}[{time_str}]{RESET} {color}{label}{RESET}  {DIM}USER INPUT{RESET}"
        elif role == "assistant":
            prefix = f"{DIM}[{time_str}]{RESET} {color}{label}{RESET}"
        elif role == "toolResult":
            prefix = f"{DIM}[{time_str}]{RESET} {color}{label}{RESET}"
        else:
            prefix = f"{DIM}[{time_str}]{RESET} {color}{label}{RESET}  {DIM}{role}{RESET}"

        print(prefix)
        for line in lines:
            for subline in line.split("\n"):
                print(f"  {subline}")
        print()

    elif t == "custom_message":
        ts = event.get("timestamp", "")
        time_str = format_ts(ts) if ts else ""
        ctype = event.get("customType", "")
        data = event.get("data", {})
        if ctype == "session-started":
            session_key = data.get("sessionKey", "")
            print(f"{DIM}[{time_str}] {color}{label}{RESET}  {CYAN}SESSION STARTED{RESET}  {DIM}{session_key}{RESET}\n")
        elif ctype == "session-ended":
            print(f"{DIM}[{time_str}] {color}{label}{RESET}  {CYAN}SESSION ENDED{RESET}\n")


class AgentWatcher:
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.current_file: str | None = None
        self.file_handle = None
        self.file_pos: int = 0

    def tick(self) -> None:
        latest = latest_session_file(self.agent_id)

        if latest != self.current_file:
            if self.file_handle:
                self.file_handle.close()
            self.current_file = latest
            self.file_pos = 0
            if latest:
                self.file_handle = open(latest, "r", encoding="utf-8")
                cfg = AGENTS[self.agent_id]
                fname = os.path.basename(latest)
                print(
                    f"\n{cfg['color']}{'─' * 60}{RESET}\n"
                    f"{cfg['color']}{cfg['label']}{RESET}  watching  {DIM}{fname}{RESET}\n"
                    f"{cfg['color']}{'─' * 60}{RESET}\n"
                )
            else:
                self.file_handle = None
                return

        if not self.file_handle:
            return

        self.file_handle.seek(self.file_pos)
        for line in self.file_handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                process_event(self.agent_id, event)
            except json.JSONDecodeError:
                pass
        self.file_pos = self.file_handle.tell()


def main() -> None:
    target_agents = list(AGENTS.keys())
    if len(sys.argv) > 1:
        arg = sys.argv[1].lower()
        if arg not in AGENTS:
            print(f"Unknown agent '{arg}'. Choose from: {', '.join(AGENTS.keys())}")
            sys.exit(1)
        target_agents = [arg]

    watchers = [AgentWatcher(a) for a in target_agents]

    print(f"\n{BOLD}Hermes Agent Watcher{RESET}  —  watching: {', '.join(target_agents)}")
    print(f"{DIM}Ctrl+C to stop{RESET}\n")

    try:
        while True:
            for w in watchers:
                w.tick()
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{RESET}")
        for w in watchers:
            if w.file_handle:
                w.file_handle.close()


if __name__ == "__main__":
    main()
