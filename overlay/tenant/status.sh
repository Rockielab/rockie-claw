#!/usr/bin/env bash
# Report tenant status. Stdout is parseable JSON.
#
# Inputs:
#   TENANT_NAME, TENANT_PORT, TENANT_HOME (all required)
#
# Output:
#   { "tenant": "<name>", "running": true|false, "pid": <int|null>, "port": <int>, "workspace": "<path>", "skills_rev": "<sha>" }

set -uo pipefail

: "${TENANT_NAME:?TENANT_NAME is required}"
: "${TENANT_PORT:?TENANT_PORT is required}"
: "${TENANT_HOME:?TENANT_HOME is required}"

PID_FILE="$TENANT_HOME/.gateway.pid"
SKILLS_DIR="$TENANT_HOME/skills"
WORKSPACE="$TENANT_HOME/workspace"

PID=""
RUNNING=false
if [ -f "$PID_FILE" ]; then
  PID=$(cat "$PID_FILE")
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    RUNNING=true
  fi
fi

# If no PID file, fall back to checking the port listener
if [ "$RUNNING" = false ]; then
  PORT_PID=$(lsof -ti :"$TENANT_PORT" 2>/dev/null || true)
  if [ -n "$PORT_PID" ]; then
    RUNNING=true
    PID="$PORT_PID"
  fi
fi

SKILLS_REV=""
if [ -d "$SKILLS_DIR/.git" ]; then
  SKILLS_REV=$(git -C "$SKILLS_DIR" rev-parse --short HEAD 2>/dev/null || echo "")
fi

cat <<EOF
{
  "tenant": "$TENANT_NAME",
  "running": $RUNNING,
  "pid": ${PID:-null},
  "port": $TENANT_PORT,
  "workspace": "$WORKSPACE",
  "skills_rev": "$SKILLS_REV"
}
EOF
