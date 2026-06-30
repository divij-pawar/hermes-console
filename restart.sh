#!/usr/bin/env bash
# Hermes Console — restart this folder's server.py (start if not running).
#
# Usage:
#   ./restart.sh           # restart or start, then report status
#   ./restart.sh --quiet   # restart or start, no status report

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

quiet=0
[[ "${1:-}" == "--quiet" ]] && quiet=1

"$SCRIPT_DIR/stop.sh" || true

args=()
(( quiet )) && args+=(--quiet)
exec "$SCRIPT_DIR/start.sh" "${args[@]}"
