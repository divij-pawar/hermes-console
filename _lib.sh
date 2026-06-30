#!/usr/bin/env bash
# Shared helpers for Hermes Console shell scripts in this directory.

set -u

if [[ -n "${BASH_SOURCE[1]:-}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
PID_FILE="$SCRIPT_DIR/.hermes-console.pid"
LOG_FILE="$SCRIPT_DIR/console.log"
SERVER="$SCRIPT_DIR/server.py"

GREEN=$'\033[32m'
YELLOW=$'\033[33m'
RED=$'\033[31m'
DIM=$'\033[2m'
RESET=$'\033[0m'

# Load .env then .env.local (shell env wins; later files do not override set vars).
load_env() {
  local file key val
  for file in "$SCRIPT_DIR/.env" "$SCRIPT_DIR/.env.local"; do
    [[ -f "$file" ]] || continue
    while IFS='=' read -r key val || [[ -n "$key" ]]; do
      [[ -z "$key" || "$key" =~ ^[[:space:]]*# ]] && continue
      key="${key#"${key%%[![:space:]]*}"}"
      key="${key%"${key##*[![:space:]]}"}"
      [[ -z "$key" ]] && continue
      val="${val%%#*}"
      val="${val#"${val%%[![:space:]]*}"}"
      val="${val%"${val##*[![:space:]]}"}"
      val="${val#\'}"; val="${val%\'}"
      val="${val#\"}"; val="${val%\"}"
      [[ -n "$key" && -z "${!key+x}" ]] && export "$key=$val"
    done < "$file"
  done
}

console_port() {
  echo "${HERMES_WEBUI_PORT:-7979}"
}

console_host() {
  echo "${HERMES_WEBUI_HOST:-127.0.0.1}"
}

console_url() {
  echo "http://$(console_host):$(console_port)"
}

pid_on_port() {
  local port="$1"
  lsof -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -1
}

read_pid_file() {
  if [[ -f "$PID_FILE" ]]; then
    cat "$PID_FILE" 2>/dev/null
  fi
}

pid_alive() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

console_pid() {
  local pid port
  pid="$(read_pid_file)"
  if pid_alive "$pid"; then
    echo "$pid"
    return 0
  fi
  port="$(console_port)"
  pid="$(pid_on_port "$port")"
  if [[ -n "$pid" ]] && [[ "$(ps -p "$pid" -o command= 2>/dev/null || true)" == *"$SERVER"* ]]; then
    echo "$pid"
    return 0
  fi
  return 1
}

is_console_running() {
  console_pid >/dev/null 2>&1
}

dashboard_reachable() {
  curl -fs -m 3 "$(console_url)/api/status" >/dev/null 2>&1
}
