#!/usr/bin/env bash
# Hermes Console — stop this folder's server.py only (gateways unchanged).
#
# Usage:
#   ./stop.sh

set -euo pipefail

source "$(cd "$(dirname "$0")" && pwd)/_lib.sh"
load_env

if ! pid="$(console_pid)"; then
  rm -f "$PID_FILE"
  echo "${DIM}Hermes Console is not running.${RESET}"
  exit 0
fi

echo "${DIM}Stopping Hermes Console (pid=$pid)...${RESET}"
kill "$pid" 2>/dev/null || true

for _ in 1 2 3 4 5; do
  pid_alive "$pid" || break
  sleep 1
done

if pid_alive "$pid"; then
  kill -9 "$pid" 2>/dev/null || true
fi

rm -f "$PID_FILE"
echo "${GREEN}  ✓ Hermes Console stopped${RESET}"
