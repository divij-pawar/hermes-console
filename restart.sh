#!/usr/bin/env bash
# Hermes Monitor — restart if running, start if not.
#
# Manages only the web-ui launchd service (ai.hermes.webui).
# Gateways are unchanged; use start-fleet.sh / stop.sh for the full fleet.
#
# Usage:
#   ./restart.sh           # restart or start, then report status
#   ./restart.sh --quiet   # restart or start, no status report

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SVC="ai.hermes.webui"
PLIST="$HOME/Library/LaunchAgents/$SVC.plist"
PORT="${HERMES_WEBUI_PORT:-7979}"
DOMAIN="gui/$(id -u)/$SVC"

GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

quiet=0
[[ "${1:-}" == "--quiet" ]] && quiet=1

launchctl_line() {
  launchctl list 2>/dev/null | awk -v s="$SVC" '$3 == s {print; exit}'
}

is_loaded() {
  [[ -n "$(launchctl_line)" ]]
}

is_running() {
  local line pid
  line="$(launchctl_line)"
  [[ -z "$line" ]] && return 1
  pid="$(awk '{print $1}' <<<"$line")"
  [[ "$pid" != "-" ]]
}

if [[ ! -f "$PLIST" ]]; then
  echo "${RED}✗ $SVC plist not found: $PLIST${RESET}"
  echo "${DIM}  Install from sf_agents/launchd/ai.hermes.webui.plist${RESET}"
  exit 1
fi

if is_running; then
  echo "${DIM}Restarting Hermes Monitor ($SVC)…${RESET}"
  if launchctl kickstart -k "$DOMAIN" 2>/dev/null; then
    echo "${GREEN}  ✓ $SVC restarted${RESET}"
  elif "$SCRIPT_DIR/stop.sh" --webui >/dev/null 2>&1 \
      && launchctl load "$PLIST" 2>/dev/null; then
    echo "${GREEN}  ✓ $SVC restarted (unload/load)${RESET}"
  else
    echo "${RED}  ✗ $SVC restart failed (see ~/.hermes/logs/webui.log)${RESET}"
    exit 1
  fi
elif is_loaded; then
  echo "${DIM}Monitor loaded but not running — starting $SVC…${RESET}"
  if launchctl kickstart "$DOMAIN" 2>/dev/null \
      || launchctl load "$PLIST" 2>/dev/null; then
    echo "${GREEN}  ✓ $SVC started${RESET}"
  else
    echo "${RED}  ✗ $SVC start failed${RESET}"
    exit 1
  fi
else
  echo "${DIM}Starting Hermes Monitor ($SVC)…${RESET}"
  if launchctl load "$PLIST" 2>/dev/null; then
    echo "${GREEN}  ✓ $SVC loaded${RESET}"
  else
    echo "${RED}  ✗ $SVC failed to load (see ~/.hermes/logs/webui.log)${RESET}"
    exit 1
  fi
fi

if (( quiet )); then
  exit 0
fi

echo "${DIM}Waiting 3s for Monitor to settle…${RESET}"
sleep 3

echo
line="$(launchctl_line)"
if [[ -n "$line" ]]; then
  pid="$(awk '{print $1}' <<<"$line")"
  status="$(awk '{print $2}' <<<"$line")"
  if [[ "$pid" == "-" ]]; then
    echo "${YELLOW}⚠ $SVC loaded but not running (status=$status)${RESET}"
  else
    echo "${GREEN}✓ $SVC running${RESET}  pid=$pid  status=$status"
  fi
else
  echo "${RED}✗ $SVC not loaded${RESET}"
fi

echo
if curl -fs -m 3 "http://localhost:$PORT/api/status" >/dev/null 2>&1; then
  echo "${GREEN}✓ Dashboard reachable:${RESET}  http://localhost:$PORT"
else
  echo "${YELLOW}⚠ Dashboard not responding yet — give it a few more seconds${RESET}"
fi
