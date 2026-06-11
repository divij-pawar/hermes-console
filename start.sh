#!/usr/bin/env bash
# Back-compat wrapper — Monitor restart/start lives in restart.sh.
exec "$(cd "$(dirname "$0")" && pwd)/restart.sh" "$@"
