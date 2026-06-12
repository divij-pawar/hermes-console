#!/usr/bin/env bash
# Hermes Monitor — Anton lab (Option A, isolated)
#
# Runs a second monitor instance pointed exclusively at the Docker lab
# container's bind-mount. Shows only lab activity — no production kanban,
# no production agents, no backup panel.
#
# Prerequisites:
#   1. Docker container is running:
#        docker compose -f docker/docker-compose.anton.yml up
#   2. Bind-mount is accessible on the host at /tmp/hermes-anton-lab/home
#
# Usage:
#   ./start-lab.sh            # foreground, Ctrl+C to stop
#   ./start-lab.sh --port 7981  # use a custom port

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env.lab if present — shell environment always takes priority
if [[ -f "$SCRIPT_DIR/.env.lab" ]]; then
  while IFS='=' read -r key val; do
    [[ -z "$key" || "$key" =~ ^# ]] && continue
    val="${val%%#*}"         # strip inline comments
    val="${val#"${val%%[![:space:]]*}"}"  # ltrim
    val="${val%"${val##*[![:space:]]}"}"  # rtrim
    val="${val#\'}" ; val="${val%\'}"     # strip single quotes
    val="${val#\"}" ; val="${val%\"}"     # strip double quotes
    [[ -n "$key" && -z "${!key+x}" ]] && export "$key=$val"
  done < "$SCRIPT_DIR/.env.lab"
fi

LAB_HOME="${HERMES_DIR:-/tmp/hermes-anton-lab/home}"
PORT="${HERMES_WEBUI_PORT:-7980}"
CONTAINER="${HERMES_DOCKER_CONTAINER:-hermes-anton-lab}"

GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

# Allow --port override
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --lab-home) LAB_HOME="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Verify the bind-mount exists and looks like a Hermes home
if [[ ! -d "$LAB_HOME" ]]; then
  echo "${RED}✗ Lab home not found: $LAB_HOME${RESET}"
  echo "${DIM}  Is the Docker container running?${RESET}"
  echo "${DIM}  docker compose -f docker/docker-compose.anton.yml up${RESET}"
  exit 1
fi

if [[ ! -d "$LAB_HOME/profiles" && ! -f "$LAB_HOME/config.yaml" ]]; then
  echo "${YELLOW}⚠ $LAB_HOME exists but looks empty — container may not be fully initialised yet${RESET}"
fi

# Warn if port is already in use
if lsof -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "${YELLOW}⚠ Port $PORT already in use — pick another with --port <n>${RESET}"
  exit 1
fi

echo "${GREEN}Hermes Lab Monitor${RESET}"
echo "${DIM}  Lab home  : $LAB_HOME${RESET}"
echo "${DIM}  Container : $CONTAINER${RESET}"
echo "${DIM}  Port      : $PORT${RESET}"
echo "${DIM}  Press Ctrl+C to stop${RESET}"
echo

exec env \
  HERMES_DIR="$LAB_HOME" \
  HERMES_WEBUI_PORT="$PORT" \
  HERMES_MONITOR_DB="$LAB_HOME/monitor.db" \
  HERMES_DOCKER_CONTAINER="$CONTAINER" \
  python3 "$SCRIPT_DIR/server.py"
