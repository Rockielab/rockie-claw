#!/usr/bin/env bash
# Multi-tenant runtime entrypoint.
#
# Selects what runs in this container based on $MODE:
#   - subscription   : official `claude` / `codex` binaries running as the
#                      tenant's Pro/Max OAuth session. The container stays
#                      alive (broker in foreground) so the platform-context
#                      proxy can drive the binaries via the PTY-WS broker.
#   - byok           : OpenClaw gateway authenticated against the tenant's
#                      Anthropic / OpenAI API key (env vars).
#   - open-weights   : OpenClaw gateway pointed at a platform-hosted
#                      open-weights endpoint (cerebras / chutes / etc).
#
# In ALL modes we start the PTY-WebSocket broker (port 7681) in the
# background so the platform-context proxy can spawn claude/codex/bash
# PTYs at any time. In `byok` / `open-weights` the OpenClaw gateway runs
# in the foreground; in `subscription` we `wait -n` so signals propagate.
#
# Any extra args passed to the container are forwarded to the chosen
# command, so `docker run ... claude --version` still works.

set -euo pipefail

MODE="${MODE:-byok}"
# OpenClaw gateway needs to listen on the Fly machine's external
# interface so platform-context can HTTP-proxy to it through the
# WireGuard tunnel. Default to 0.0.0.0; OPENCLAW_GATEWAY_TOKEN gates
# the endpoint so this is safe (auth-required, not open).
OPENCLAW_BIND="${OPENCLAW_BIND:-0.0.0.0}"
# Match the gateway's documented default (src/gateway/server.impl.ts:508)
# so platform-context's OpenClawGatewayBackend can hit it without per-
# tenant port config.
OPENCLAW_PORT="${OPENCLAW_PORT:-18789}"
BROKER_PORT="${BROKER_PORT:-7681}"

log() {
  printf '[entrypoint] %s\n' "$*" >&2
}

# If the caller passed a command (e.g. `docker run image claude --version`),
# just run it. The mode router only kicks in when no command is given.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# --- broker (always-on) -----------------------------------------------------
if [ -x /usr/local/bin/broker ]; then
  if [ -z "${BROKER_TENANT_TOKEN:-}" ]; then
    log "WARN: BROKER_TENANT_TOKEN unset; broker /ws + /spawn will refuse all requests."
  else
    log "broker: tenant token is set (length-only check)"
  fi
  log "broker: starting on :${BROKER_PORT}"
  /usr/local/bin/broker &
  BROKER_PID=$!
else
  log "WARN: /usr/local/bin/broker not present; skipping."
  BROKER_PID=
fi

case "$MODE" in
  subscription)
    log "MODE=subscription; tenant uses official claude/codex CLIs via OAuth."
    log "Available binaries: $(command -v claude || echo 'claude MISSING') / $(command -v codex || echo 'codex MISSING')"
    # The broker is the only foreground process; wait -n so SIGTERM kills it.
    if [ -n "${BROKER_PID:-}" ]; then
      wait -n "$BROKER_PID"
    else
      exec tail -f /dev/null
    fi
    ;;
  byok|open-weights)
    log "MODE=${MODE}; starting OpenClaw gateway on ${OPENCLAW_BIND}:${OPENCLAW_PORT}."

    # Gateway token: platform-context's OpenClawGatewayBackend sends
    # `Authorization: Bearer $OPENCLAW_GATEWAY_TOKEN`. Reuse the broker
    # token if no dedicated one is set (same per-tenant secret, same
    # role: proxy auth from platform-context). Without a token, the
    # gateway accepts unauthenticated requests, which is wrong on a
    # public Fly machine.
    if [ -z "${OPENCLAW_GATEWAY_TOKEN:-}" ] && [ -n "${BROKER_TENANT_TOKEN:-}" ]; then
      export OPENCLAW_GATEWAY_TOKEN="$BROKER_TENANT_TOKEN"
      log "openclaw: gateway token reused from BROKER_TENANT_TOKEN"
    elif [ -n "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
      log "openclaw: dedicated gateway token set"
    else
      log "WARN: no OPENCLAW_GATEWAY_TOKEN and no BROKER_TENANT_TOKEN — gateway will be open."
    fi

    cd /app

    # Render the openclaw config so the gateway loads mcp-rockie at boot.
    # The Commander definition for `openclaw gateway` (src/cli/gateway-cli/
    # run.ts) does NOT declare a --config flag; the gateway reads
    # $OPENCLAW_CONFIG_PATH (src/gateway/call.ts resolveGatewayConfigPath
    # → src/config/paths.ts) instead. envsubst isn't installed in this
    # image (Dockerfile.multitenant installs only jq+friends), so we
    # render via `jq -n --arg` from a fixed env allowlist. If any of
    # the four vars are unset, mcp-rockie spawns but tool calls return
    # auth errors — that's distinct from "no tools loaded" (#24).
    OPENCLAW_CFG_DIR=/home/runtime/.openclaw
    OPENCLAW_CFG="$OPENCLAW_CFG_DIR/openclaw.json"
    mkdir -p "$OPENCLAW_CFG_DIR"
    jq -n \
      --arg api_base "${ROCKIELAB_API_BASE:-}" \
      --arg api_password "${ROCKIELAB_API_PASSWORD:-}" \
      --arg notebook_password "${OPEN_NOTEBOOK_PASSWORD:-}" \
      --arg tenant_token "${ROCKIELAB_TENANT_DEV_TOKEN:-}" \
      '{
        mcp: {
          servers: {
            rockie: {
              command: "node",
              args: ["/home/runtime/mcp-rockie/server.js"],
              env: {
                ROCKIELAB_API_BASE: $api_base,
                ROCKIELAB_API_PASSWORD: $api_password,
                OPEN_NOTEBOOK_PASSWORD: $notebook_password,
                ROCKIELAB_TENANT_DEV_TOKEN: $tenant_token
              }
            }
          }
        }
      }' > "$OPENCLAW_CFG"
    export OPENCLAW_CONFIG_PATH="$OPENCLAW_CFG"
    log "openclaw: rendered $OPENCLAW_CFG with $(jq '.mcp.servers | length' "$OPENCLAW_CFG") MCP servers; OPENCLAW_CONFIG_PATH exported"

    GATEWAY_ARGS=(--port "$OPENCLAW_PORT" --bind "$OPENCLAW_BIND")
    if [ -n "${OPENCLAW_GATEWAY_TOKEN:-}" ]; then
      GATEWAY_ARGS+=(--token "$OPENCLAW_GATEWAY_TOKEN")
    else
      GATEWAY_ARGS+=(--auth none)
    fi
    # NOTE: --allow-unconfigured deliberately omitted — we now ship a
    # rendered config via $OPENCLAW_CONFIG_PATH. If a future change
    # reverts to --allow-unconfigured, scripts/check-multitenant-byok-config.mjs
    # will fail in CI.
    exec node /app/dist/index.js gateway "${GATEWAY_ARGS[@]}"
    ;;
  *)
    log "ERROR: unknown MODE=${MODE}; expected one of: subscription, byok, open-weights"
    exit 64
    ;;
esac
