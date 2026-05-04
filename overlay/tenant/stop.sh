#!/usr/bin/env bash
# Stop a tenant gateway. Idempotent.
#
# Inputs (env vars):
#   TENANT_NAME         (required) — used to find the running daemon
#   TENANT_PORT         (required) — used as a fallback if no PID is recorded
#
# Strategy:
#   1. If $TENANT_HOME/.gateway.pid exists, send SIGTERM, wait up to 30s, SIGKILL on timeout
#   2. Else find listener on TENANT_PORT and kill that pid
#   3. Always exit 0

set -uo pipefail

: "${TENANT_NAME:?TENANT_NAME is required}"
: "${TENANT_PORT:?TENANT_PORT is required}"
: "${TENANT_HOME:?TENANT_HOME is required}"

PID_FILE="$TENANT_HOME/.gateway.pid"

graceful_kill() {
  local pid="$1"
  kill -TERM "$pid" 2>/dev/null || return 0
  for _ in $(seq 1 30); do
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  done
  kill -KILL "$pid" 2>/dev/null || true
}

if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE")
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    graceful_kill "$PID"
  fi
  rm -f "$PID_FILE"
fi

# Fallback: kill anything listening on the port
PORT_PID=$(lsof -ti :"$TENANT_PORT" 2>/dev/null || true)
if [ -n "$PORT_PID" ]; then
  graceful_kill "$PORT_PID"
fi

exit 0
