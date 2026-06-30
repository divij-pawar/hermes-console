#!/usr/bin/env bash
# Hermes Console — start this folder's server.py if not already running.
#
# Usage:
#   ./start.sh           # start in background, then report status
#   ./start.sh --quiet   # start in background, no status report

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
load_env

quiet=0
[[ "${1:-}" == "--quiet" ]] && quiet=1

if is_console_running; then
  pid="$(console_pid)"
  (( quiet )) || echo "${YELLOW}⚠ Hermes Console already running${RESET}  pid=$pid  $(console_url)"
  exit 0
fi

if [[ ! -f "$SERVER" ]]; then
  echo "${RED}✗ server.py not found: $SERVER${RESET}"
  exit 1
fi

port="$(console_port)"
if existing_pid="$(pid_on_port "$port")"; then
  if [[ "$(ps -p "$existing_pid" -o command= 2>/dev/null || true)" != *"$SERVER"* ]]; then
    echo "${RED}✗ Port $port is in use by another process (pid=$existing_pid)${RESET}"
    exit 1
  fi
fi

echo "${DIM}Starting Hermes Console from ${SCRIPT_DIR}...${RESET}"
nohup python3 "$SERVER" >>"$LOG_FILE" 2>&1 &
pid=$!
echo "$pid" >"$PID_FILE"

sleep 1
if ! pid_alive "$pid"; then
  echo "${RED}✗ Console failed to start — see $LOG_FILE${RESET}"
  rm -f "$PID_FILE"
  exit 1
fi

echo "${GREEN}  ✓ Hermes Console started${RESET}  pid=$pid  $(console_url)"
echo "${DIM}  Log: $LOG_FILE${RESET}"

(( quiet )) && exit 0

sleep 2
if dashboard_reachable; then
  echo "${GREEN}✓ Dashboard reachable:${RESET}  $(console_url)"
else
  echo "${YELLOW}⚠ Dashboard not responding yet — give it a few more seconds${RESET}"
fi
