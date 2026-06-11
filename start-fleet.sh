#!/usr/bin/env bash
# Hermes fleet — start Sage + Imagine gateways and the Monitor dashboard.
#
# Services started (all via launchd, restart-on-crash):
#   ai.hermes.gateway          → Sage gateway (Slack bot + kanban dispatcher)
#   ai.hermes.gateway-imagine  → Imagine gateway (Slack bot)
#   ai.hermes.webui            → web-ui dashboard on http://localhost:7979
#
# For Monitor-only restart/start, use ./restart.sh instead.
#
# Usage:
#   ./start-fleet.sh             # start everything, then report status
#   ./start-fleet.sh --quiet     # start everything, no status report

set -u

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

SERVICES=(
  "ai.hermes.gateway"
  "ai.hermes.gateway-imagine"
  "ai.hermes.webui"
)

GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

quiet=0
[[ "${1:-}" == "--quiet" ]] && quiet=1

echo "${DIM}Loading Hermes services from $LAUNCH_AGENTS${RESET}"

for svc in "${SERVICES[@]}"; do
  plist="$LAUNCH_AGENTS/$svc.plist"
  if [[ ! -f "$plist" ]]; then
    echo "${YELLOW}  ⚠ $svc — plist not found, skipping (${plist})${RESET}"
    continue
  fi

  if launchctl list | grep -q "[[:space:]]$svc\$"; then
    echo "${DIM}  • $svc already loaded${RESET}"
  else
    if launchctl load "$plist" 2>/dev/null; then
      echo "${GREEN}  ✓ $svc loaded${RESET}"
    else
      echo "${RED}  ✗ $svc failed to load (see Console.app or webui.log)${RESET}"
    fi
  fi
done

if (( quiet )); then
  exit 0
fi

echo "${DIM}Waiting 5s for services to settle…${RESET}"
sleep 5

echo
echo "${GREEN}Hermes fleet status:${RESET}"
launchctl list | awk -v g="$GREEN" -v r="$RESET" -v d="$DIM" '
  /ai\.hermes\./ {
    pid = $1
    status = $2
    label = $3
    if (pid == "-") {
      printf "  %s%-32s%s pid=  status=%s %s(stopped)%s\n", d, label, r, status, d, r
    } else {
      printf "  %s%-32s%s pid=%-6s status=%s\n", g, label, r, pid, status
    }
  }
'

PORT="${HERMES_WEBUI_PORT:-7979}"
echo
if curl -fs -m 3 "http://localhost:$PORT/api/status" >/dev/null 2>&1; then
  echo "${GREEN}✓ Dashboard reachable:${RESET}  http://localhost:$PORT"
else
  echo "${YELLOW}⚠ Dashboard not responding yet — give it a few more seconds${RESET}"
fi

echo
echo "${DIM}Last Slack-connect line per gateway log:${RESET}"
for log in \
    "$HOME/.hermes/logs/gateway.log" \
    "$HOME/.hermes/profiles/imagine/logs/gateway.log"; do
  if [[ -f "$log" ]]; then
    last=$(grep "slack connected" "$log" 2>/dev/null | tail -1)
    if [[ -n "$last" ]]; then
      echo "  ${log#$HOME/}:"
      echo "    ${DIM}${last}${RESET}"
    else
      echo "  ${log#$HOME/}: ${YELLOW}no slack-connected entry yet${RESET}"
    fi
  fi
done
