#!/usr/bin/env bash
# Hermes fleet — stop the gateways + dashboard.
#
# Unloads the launchd services so they don't restart. To start the Monitor
# again, run ./restart.sh (or ./start.sh). For the full fleet, use start-fleet.sh.
#
# Usage:
#   ./stop.sh             # unload everything
#   ./stop.sh --webui     # unload only the dashboard (keep agents online)
#   ./stop.sh --agents    # unload only sage + imagine gateways (keep ui)

set -u

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

ALL_SERVICES=(
  "ai.hermes.gateway-imagine"
  "ai.hermes.gateway"
  "ai.hermes.webui"
)

WEBUI_ONLY=("ai.hermes.webui")
AGENTS_ONLY=("ai.hermes.gateway-imagine" "ai.hermes.gateway")

mode="${1:-all}"
case "$mode" in
  --webui)   targets=("${WEBUI_ONLY[@]}");;
  --agents)  targets=("${AGENTS_ONLY[@]}");;
  all|"")    targets=("${ALL_SERVICES[@]}");;
  -h|--help)
    echo "Usage: ./stop.sh [--webui|--agents]"
    echo "  no args   stop everything"
    echo "  --webui   stop only the dashboard"
    echo "  --agents  stop only sage + imagine gateways"
    exit 0
    ;;
  *)
    echo "Unknown argument: $mode (use --help)"
    exit 1
    ;;
esac

GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

for svc in "${targets[@]}"; do
  plist="$LAUNCH_AGENTS/$svc.plist"
  if [[ ! -f "$plist" ]]; then
    echo "${YELLOW}  ⚠ $svc — plist not found, nothing to stop${RESET}"
    continue
  fi

  if launchctl list | grep -q "[[:space:]]$svc\$"; then
    if launchctl unload "$plist" 2>/dev/null; then
      echo "${GREEN}  ✓ $svc stopped${RESET}"
    else
      echo "${RED}  ✗ $svc unload failed${RESET}"
    fi
  else
    echo "${DIM}  • $svc was not running${RESET}"
  fi
done

# Brief status check so the user sees what's left
echo
remaining=$(launchctl list | grep "ai\.hermes\." || true)
if [[ -z "$remaining" ]]; then
  echo "${DIM}No Hermes services running.${RESET}"
else
  echo "Still running:"
  echo "$remaining" | awk -v g="$GREEN" -v r="$RESET" '{ printf "  %s%-32s%s pid=%-6s\n", g, $3, r, $1 }'
fi
