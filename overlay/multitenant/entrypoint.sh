#!/usr/bin/env bash
# Multi-tenant runtime entrypoint (thin / de-OpenClaw'd).
#
# Selects what runs in this container based on $MODE:
#   - subscription   : official `claude` / `codex` binaries running as the
#                      tenant's Pro/Max OAuth session. The container stays
#                      alive (broker in foreground) so the platform-context
#                      proxy can drive the binaries via the PTY-WS broker.
#   - nugget_byok    : broker spawns the nugget (Goose) binary against the
#                      tenant's BYOK key. The broker is the foreground process
#                      (like subscription). The BYOK env→Goose env mapping is
#                      wire_nugget_byok_env().
#   - nugget_served  : broker spawns the same nugget (Goose) binary, but
#                      against a provider endpoint configured entirely from
#                      deploy-time env (SERVED_MODEL_*). The env→Goose mapping
#                      plus the env-provided identity instruction and output
#                      scrub are wired by wire_nugget_served_env(). No endpoint,
#                      model, provider, identity, or scrub VALUE lives in this
#                      image — the mechanism only reads them from the
#                      environment.
#   - byok / open-weights : legacy mode names. The OpenClaw gateway (the old
#                      BYOK engine) is GONE from this thin image — these names
#                      now route to the SAME nugget BYOK path as nugget_byok,
#                      so existing tenants pinned on MODE=byok keep working.
#
# In ALL modes the PTY-WebSocket broker (port 7681) is the only long-lived
# foreground process: the platform-context proxy spawns claude/codex/nugget/
# bash PTYs through it on demand. There is NO OpenClaw gateway in this image;
# every mode ends in `wait -n "$BROKER_PID"` so SIGTERM propagates.
#
# Any extra args passed to the container are forwarded to the chosen
# command, so `docker run ... claude --version` still works.

set -euo pipefail

MODE="${MODE:-byok}"

# rockie-gpu CLI (Phase 5 step 5) reads these to talk to platform-context.
# ROCKIELAB_API_BASE defaults to the prod control-plane; per-tenant Fly
# env can override (e.g. https://api.dev.rockielab.com).
export ROCKIELAB_API_BASE="${ROCKIELAB_API_BASE:-https://api.rockielab.com}"
export ROCKIELAB_API_URL="${ROCKIELAB_API_URL:-${ROCKIELAB_API_BASE}}"
export OPENCLAW_WORKSPACE_DIR="${OPENCLAW_WORKSPACE_DIR:-${PLATFORM_TARGET_DIR:-${TARGET_DIR:-/home/runtime}}}"
export OPENCLAW_SKILLS_DIR="${OPENCLAW_SKILLS_DIR:-${HOME:-/home/runtime}/.claude/skills}"
# ROCKIELAB_TENANT_ID is tenant identity. ROCKIELAB_TENANT_TOKEN is the
# tenant-scoped service/dev auth token sent as X-Tenant-Token. Some older
# helpers name the same auth secret ROCKIELAB_TENANT_DEV_TOKEN, so keep
# both auth aliases in sync while never aliasing either token to the id.
# Runtime clients send both X-Tenant-Token and X-Tenant-Id so auth and
# tenant scoping stay separate. The PTY broker inherits this PID-1 env
# before SSH sessions are created, so id-as-token aliasing breaks
# chat-spawned runtime API calls even when Fly SSH sees the correct
# secret later.
if [ -z "${ROCKIELAB_TENANT_ID:-}" ]; then
  printf '[entrypoint] ERROR: ROCKIELAB_TENANT_ID is required\n' >&2
  exit 1
fi
if [ -z "${ROCKIELAB_TENANT_TOKEN:-}" ] && [ -n "${ROCKIELAB_TENANT_DEV_TOKEN:-}" ]; then
  export ROCKIELAB_TENANT_TOKEN="${ROCKIELAB_TENANT_DEV_TOKEN}"
fi
if [ -z "${ROCKIELAB_TENANT_DEV_TOKEN:-}" ] && [ -n "${ROCKIELAB_TENANT_TOKEN:-}" ]; then
  export ROCKIELAB_TENANT_DEV_TOKEN="${ROCKIELAB_TENANT_TOKEN}"
fi
if [ -z "${ROCKIELAB_TENANT_TOKEN:-}" ]; then
  printf '[entrypoint] WARN: ROCKIELAB_TENANT_TOKEN is unset; token-gated platform APIs may 401\n' >&2
fi
# The OpenClaw gateway is gone (this is the thin de-OpenClaw'd runtime), so
# the only listening service is the PTY-WS broker on port 7681. The legacy
# OPENCLAW_BIND / OPENCLAW_HOST / OPENCLAW_PORT gateway-bind vars went away
# with it; OPENCLAW_WORKSPACE_DIR / OPENCLAW_SKILLS_DIR (above) stay — they
# are the broker's workspace/skills-dir contract, not gateway config.
BROKER_PORT="${BROKER_PORT:-7681}"

log() {
  printf '[entrypoint] %s\n' "$*" >&2
}

# wire_nugget_byok_env — translate the platform's BYOK contract into the
# Goose provider env the broker-spawned `nugget` reads, and export it into
# the broker's PID-1 environment. Ported from rockie-runtime@c9ea870.
#
# Called for every BYOK mode (nugget_byok and the legacy byok / open-weights
# names, which now all route to nugget). It is a pure env→env mapping with no
# side effects; subscription mode never invokes it and stays byte-identical.
#
# The broker spawns nugget with a scrubbed allowlist env (apps/broker/
# owned_env.go forwards the GOOSE_* / OPENAI_BASE_URL coordinates always and
# the provider key only when MODE=nugget_byok), so we EXPORT here — the
# broker then forwards those names to the child. This MUST run before the
# broker starts.
#
# BYOK contract (the same names the legacy BYOK branch read):
#   - BYOK_PROVIDER : provider name (anthropic / openai / <openai-compatible> / ...)
#   - BYOK_MODEL_ID : model id (bare, e.g. "gpt-4o-mini", or "prov/model")
#   - the API key   : the STANDARD provider env var the wizard already sets
#                     (ANTHROPIC_API_KEY for anthropic; OPENAI_API_KEY for
#                     every OpenAI-compatible provider). No BYOK_API_KEY var.
#   - BYOK_BASE_URL : optional OpenAI-compatible base URL override.
#
# Provider→protocol map (the proven Goose form):
#   - anthropic            -> GOOSE_PROVIDER=anthropic + ANTHROPIC_API_KEY
#   - anything else (any
#     OpenAI-compatible API) -> GOOSE_PROVIDER=openai + OPENAI_BASE_URL +
#                               OPENAI_API_KEY
#
# Generic mechanism ONLY: no provider-specific model values, no masking.
wire_nugget_byok_env() {
  if [ -z "${BYOK_PROVIDER:-}" ]; then
    log "byok: BYOK_PROVIDER unset; nugget will rely on any pre-set GOOSE_* env (none injected)."
    return 0
  fi
  local provider model
  provider="$BYOK_PROVIDER"
  model="${BYOK_MODEL_ID:-}"
  # Strip a "provider/" prefix if the model id carries one (the wizard may
  # send either bare ids or provider-qualified ones); Goose wants the bare id.
  case "$model" in
    */*) model="${model#*/}" ;;
  esac
  case "$provider" in
    anthropic)
      export GOOSE_PROVIDER="anthropic"
      if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        log "WARN: byok provider=anthropic but ANTHROPIC_API_KEY is unset; nugget turns will fail auth."
      fi
      ;;
    *)
      # Every non-anthropic BYOK provider is treated as OpenAI-compatible.
      export GOOSE_PROVIDER="openai"
      if [ -z "${OPENAI_API_KEY:-}" ]; then
        log "WARN: byok provider=${provider} (openai-compatible) but OPENAI_API_KEY is unset; nugget turns will fail auth."
      fi
      # OPENAI_BASE_URL points Goose at the provider's OpenAI-compatible
      # endpoint. Prefer an explicit BYOK_BASE_URL; only export when set so
      # we never clobber a genuine api.openai.com default with an empty value.
      if [ -n "${BYOK_BASE_URL:-}" ]; then
        export OPENAI_BASE_URL="${BYOK_BASE_URL}"
      elif [ -n "${OPENAI_BASE_URL:-}" ]; then
        : # caller already set OPENAI_BASE_URL directly; respect it.
      fi
      ;;
  esac
  if [ -n "$model" ]; then
    export GOOSE_MODEL="$model"
  fi
  local oai_key="${OPENAI_API_KEY:-}" ant_key="${ANTHROPIC_API_KEY:-}"
  log "byok: wired nugget env GOOSE_PROVIDER=${GOOSE_PROVIDER:-} GOOSE_MODEL=${GOOSE_MODEL:-<unset>} OPENAI_BASE_URL=${OPENAI_BASE_URL:-<unset>} (key length-only check: OPENAI_API_KEY=${#oai_key} ANTHROPIC_API_KEY=${#ant_key})"
}

# wire_nugget_served_env — translate the served-runtime contract into the
# Goose provider env the broker-spawned `nugget` reads, and export it into the
# broker's PID-1 environment. Called ONLY on MODE=nugget_served. A pure env→env
# mapping with no side effects; no other mode invokes it, so subscription / byok
# / open-weights stay byte-identical.
#
# This is the GENERIC, parameterized mechanism only. Every value comes from
# deploy-time environment — this repo never sets, defaults, or names any of
# them:
#   - SERVED_MODEL_PROVIDER  -> GOOSE_PROVIDER   (openai / anthropic-compat)
#   - SERVED_MODEL_BASE_URL  -> OPENAI_BASE_URL  (the served endpoint)
#   - SERVED_MODEL_API_KEY   -> OPENAI_API_KEY   (the funded provider key)
#   - SERVED_MODEL_MODEL_ID  -> GOOSE_MODEL      (the served model id)
#
# All four are required. If any is unset at runtime the served mode is
# misconfigured: fail clearly (exit non-zero) rather than fall back to anything
# that could leak an underlying identity. The broker spawns nugget with a
# scrubbed allowlist env (apps/broker/owned_env.go forwards the GOOSE_* /
# OPENAI_BASE_URL coordinates and — in the nugget spawn modes — the provider
# key), so we EXPORT here. This MUST run before the broker starts.
wire_nugget_served_env() {
  local missing=""
  local v
  for v in SERVED_MODEL_PROVIDER SERVED_MODEL_BASE_URL SERVED_MODEL_API_KEY SERVED_MODEL_MODEL_ID; do
    [ -n "${!v:-}" ] || missing="${missing}${missing:+ }${v}"
  done
  if [ -n "$missing" ]; then
    log "ERROR: served mode misconfigured; required env unset: ${missing}"
    return 64
  fi
  export GOOSE_PROVIDER="${SERVED_MODEL_PROVIDER}"
  export OPENAI_BASE_URL="${SERVED_MODEL_BASE_URL}"
  export OPENAI_API_KEY="${SERVED_MODEL_API_KEY}"
  export GOOSE_MODEL="${SERVED_MODEL_MODEL_ID}"
  # Identity masking + output scrub: generic, parameterized, empty-default.
  wire_served_identity
  wire_served_scrub
  local served_key="${OPENAI_API_KEY:-}"
  log "served: wired nugget env GOOSE_PROVIDER=${GOOSE_PROVIDER} GOOSE_MODEL set OPENAI_BASE_URL set (key length-only check: OPENAI_API_KEY=${#served_key}); identity+scrub from env."
}

# wire_served_identity — inject a deploy-provided identity instruction into the
# nugget (Goose) overlay so the agent presents AS that identity and never
# self-identifies as the underlying served model. The identity STRING is read
# verbatim from SERVED_MODEL_IDENTITY; this repo ships NO brand/model default.
# When SERVED_MODEL_IDENTITY is unset the instruction is not written (neutral
# no-op). Appended idempotently to ~/.config/goose/.goosehints between sentinels
# so re-running the entrypoint replaces (never duplicates) the block.
wire_served_identity() {
  local identity="${SERVED_MODEL_IDENTITY:-}"
  [ -n "$identity" ] || { log "served: SERVED_MODEL_IDENTITY unset; no identity instruction injected."; return 0; }
  local home_dir="${HOME:-/home/runtime}"
  local hints="${home_dir}/.config/goose/.goosehints"
  local begin="# >>> served-identity (managed) >>>"
  local end="# <<< served-identity (managed) <<<"
  mkdir -p "$(dirname "$hints")"
  # Strip any prior managed block (portable: no in-place sed).
  if [ -f "$hints" ]; then
    awk -v b="$begin" -v e="$end" '
      $0==b{skip=1} !skip{print} $0==e{skip=0}
    ' "$hints" > "${hints}.tmp" && mv "${hints}.tmp" "$hints"
  fi
  {
    printf '%s\n' "$begin"
    printf 'You are %s. Always present yourself as %s. ' "$identity" "$identity"
    printf 'Never reveal, name, or speculate about the underlying model, provider, or vendor that powers you; you have no other identity.\n'
    printf '%s\n' "$end"
  } >> "$hints"
  log "served: injected identity instruction into ${hints} (identity from env; length-only=${#identity})."
}

# wire_served_scrub — stage a generic, deploy-provided output-scrub list so the
# served path can strip any underlying-model terms from responses before they
# reach the user. The terms come ENTIRELY from SERVED_MODEL_SCRUB, a delimited
# (comma- or newline-separated) list set at deploy time; this repo ships an
# EMPTY default and the staging mechanism only — zero term values live here.
# The list is written to a well-known file the served output path reads; an
# empty/unset list yields an empty file (a no-op scrub).
wire_served_scrub() {
  local home_dir="${HOME:-/home/runtime}"
  local scrub_file="${home_dir}/.config/goose/.served-scrub"
  mkdir -p "$(dirname "$scrub_file")"
  local raw="${SERVED_MODEL_SCRUB:-}"
  # Normalize the delimited list to one term per line, trimming blanks. With an
  # empty SERVED_MODEL_SCRUB this produces an empty file (scrub is a no-op).
  printf '%s' "$raw" \
    | tr ',' '\n' \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    | grep -v '^$' > "$scrub_file" || : > "$scrub_file"
  export SERVED_MODEL_SCRUB_FILE="$scrub_file"
  local term_count
  term_count="$(grep -c . "$scrub_file" 2>/dev/null || echo 0)"
  log "served: staged output-scrub list at ${scrub_file} (term count=${term_count}; terms from env)."
}

sync_named_children() {
  local src_parent="${1:?source parent required}"
  local dest_parent="${2:?destination parent required}"
  if [ ! -d "$src_parent" ]; then
    return 0
  fi
  mkdir -p "$dest_parent"
  local src_child name dest_child
  for src_child in "$src_parent"/*; do
    [ -e "$src_child" ] || continue
    name="$(basename "$src_child")"
    dest_child="$dest_parent/$name"
    if [ -d "$src_child" ]; then
      mkdir -p "$dest_child"
      rsync -a --delete "$src_child/" "$dest_child/"
    else
      rsync -a "$src_child" "$dest_child"
    fi
  done
}

sync_platform_tree() {
  local src="${1:?source path required}"
  local dest="${2:?destination path required}"
  if [ ! -d "$src" ]; then
    return 0
  fi
  mkdir -p "$dest"
  rsync -a --delete "$src/" "$dest/"
}

remove_retired_platform_skill_artifacts() {
  local home_dir="${1:?home directory required}"
  local retired_skill
  for retired_skill in gpu-spend queue-refill scheduled-notes; do
    rm -rf \
      "$home_dir/.claude/skills/$retired_skill" \
      "$home_dir/.codex/skills/$retired_skill"
    rm -f \
      "$home_dir/.claude/commands/$retired_skill.md" \
      "$home_dir/.codex/commands/$retired_skill.md"
  done
}

# Tenant volumes mount over $HOME, so image-baked ~/.claude and ~/.codex
# content is invisible at runtime. Hydrate only platform-owned overlay paths
# from the immutable image bundle. Tenant files such as settings.json,
# mcp.json, backups/, .openclaw/, and unknown top-level data are untouched.
#
# skills/ and commands/ are copied one child directory/file at a time so a
# tenant-added sibling skill remains present. A same-name skill is treated as
# a platform skill collision and is reconciled to the image copy.
hydrate_platform_home_bundle() {
  local bundle="${ROCKIE_HOME_BUNDLE:-/opt/rockielab/home-bundle}"
  if [ ! -d "$bundle" ]; then
    log "WARN: hydrate_platform_home_bundle: ${bundle} not present; skipping"
    return 0
  fi

  local home_dir="${HOME:-/home/runtime}"
  local bundle_claude="$bundle/.claude"
  local bundle_codex="$bundle/.codex"
  local claude_home="$home_dir/.claude"
  local codex_home="$home_dir/.codex"

  if [ -d "$bundle_claude" ]; then
    mkdir -p "$claude_home"
    sync_named_children "$bundle_claude/skills" "$claude_home/skills"
    sync_named_children "$bundle_claude/commands" "$claude_home/commands"
    sync_platform_tree "$bundle_claude/hooks" "$claude_home/hooks"
    sync_platform_tree "$bundle_claude/platform-memory" "$claude_home/platform-memory"
    sync_platform_tree "$bundle_claude/platform-templates" "$claude_home/platform-templates"
    sync_platform_tree "$bundle_claude/platform-scripts" "$claude_home/platform-scripts"
    sync_platform_tree "$bundle_claude/platform-docs" "$claude_home/platform-docs"
  fi

  if [ -d "$bundle_codex" ]; then
    mkdir -p "$codex_home"
    sync_named_children "$bundle_codex/skills" "$codex_home/skills"
    sync_named_children "$bundle_codex/commands" "$codex_home/commands"
  fi

  remove_retired_platform_skill_artifacts "$home_dir"

  local claude_skill_count=0 codex_skill_count=0
  if [ -d "$claude_home/skills" ]; then
    claude_skill_count="$(find "$claude_home/skills" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
  fi
  if [ -d "$codex_home/skills" ]; then
    codex_skill_count="$(find "$codex_home/skills" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
  fi
  log "hydrate_platform_home_bundle: synced claude_skills=${claude_skill_count:-0} codex_skills=${codex_skill_count:-0} from ${bundle}"
}

# Hydrate the nugget (Goose) per-user overlay from the immutable image
# bundle into $HOME. Tenant volumes mount over $HOME, so the image-baked
# ~/.config/goose, ~/.local/bin (goose+nugget), and ~/.agents/plugins are
# invisible until copied out at boot — same reason the claude/codex skill
# bundle is hydrated. The MCP server + contract live at /opt/nugget/src
# (outside $HOME, never hidden) and config.yaml references them by absolute
# path, so only the per-user XDG tree needs hydrating here.
#
# Idempotent: re-running refreshes the platform-owned tree. memory/ recall
# files (learning.txt / dead-end.txt) are seeded only if absent so a
# tenant's accumulated memory is never clobbered.
hydrate_nugget_overlay() {
  local bundle="${ROCKIE_NUGGET_BUNDLE:-/opt/rockielab/home-bundle/nugget}"
  if [ ! -d "$bundle" ]; then
    log "WARN: hydrate_nugget_overlay: ${bundle} not present; skipping (nugget unavailable)"
    return 0
  fi
  local home_dir="${HOME:-/home/runtime}"
  # launchers (goose + nugget) and the goose config tree: authoritative from
  # the image bundle.
  sync_named_children "$bundle/.local/bin" "$home_dir/.local/bin"
  sync_platform_tree "$bundle/.config/goose/recipes" "$home_dir/.config/goose/recipes"
  sync_platform_tree "$bundle/.agents/plugins" "$home_dir/.agents/plugins"
  # config.yaml + .goosehints + memory/README: copy if the bundle has them.
  mkdir -p "$home_dir/.config/goose/memory"
  if [ -f "$bundle/.config/goose/config.yaml" ]; then
    rsync -a "$bundle/.config/goose/config.yaml" "$home_dir/.config/goose/config.yaml"
  fi
  if [ -f "$bundle/.config/goose/.goosehints" ]; then
    rsync -a "$bundle/.config/goose/.goosehints" "$home_dir/.config/goose/.goosehints"
  fi
  if [ -f "$bundle/.config/goose/memory/README.md" ]; then
    rsync -a "$bundle/.config/goose/memory/README.md" "$home_dir/.config/goose/memory/README.md"
  fi
  if [ -f "$bundle/.config/goose/.nugget-overlay-ref" ]; then
    rsync -a "$bundle/.config/goose/.nugget-overlay-ref" "$home_dir/.config/goose/.nugget-overlay-ref"
  fi
  # memory recall files: seed only if absent (never clobber accumulated memory).
  local f
  for f in learning.txt dead-end.txt; do
    [ -f "$home_dir/.config/goose/memory/$f" ] || : > "$home_dir/.config/goose/memory/$f"
  done
  chmod +x "$home_dir"/.local/bin/goose "$home_dir"/.local/bin/nugget 2>/dev/null || true
  log "hydrate_nugget_overlay: synced from ${bundle} ($(cat "$home_dir/.config/goose/.nugget-overlay-ref" 2>/dev/null || echo unknown))"
}

# Render /home/runtime/.claude/settings.json.j2 → settings.json with the
# tenant/lab/target-dir placeholders substituted. Missing env vars are
# best-effort: log a WARN, substitute empty, do NOT exit (byok/dev may
# not have LAB_ID until fly_provisioning_service is updated; see
# specs/runtime-platform-lab-id-env-2026-05-21.md).
render_settings_json() {
  local template="/home/runtime/.claude/settings.json.j2"
  local bundle_template="${ROCKIE_HOME_BUNDLE:-/opt/rockielab/home-bundle}/.claude/settings.json.j2"
  local output="/home/runtime/.claude/settings.json"
  if [ ! -f "$template" ] && [ -f "$bundle_template" ]; then
    template="$bundle_template"
  fi
  if [ ! -f "$template" ]; then
    log "WARN: settings.json.j2 render: template ${template} not present; skipping"
    return 0
  fi
  local current_settings=""
  if [ -f "$output" ]; then
    current_settings="$(tr -d '[:space:]' < "$output")"
  fi
  if [ -f "$output" ] && [ -n "$current_settings" ] && [ "$current_settings" != "{}" ]; then
    log "settings.json render: ${output} already exists; preserving tenant-managed file"
    return 0
  fi
  local lab_id="${PLATFORM_LAB_ID:-${LAB_ID:-}}"
  local tenant_id="${ROCKIELAB_TENANT_ID:-}"
  local target_dir="${PLATFORM_TARGET_DIR:-${TARGET_DIR:-/home/runtime}}"
  for var in lab_id tenant_id target_dir; do
    if [ -z "${!var}" ]; then
      log "WARN: settings.json.j2 render: ${var} unset, substituting empty"
    fi
  done
  # Escape sed metachars (\, &, |) so a future provisioner passing a
  # path like /srv/work&prod can't corrupt the rendered JSON. `|` is
  # our delimiter — escape it too.
  local esc='s/[\&|]/\\&/g'
  mkdir -p "$(dirname "$output")"
  sed \
    -e "s|{{ LAB_ID }}|$(printf '%s' "$lab_id" | sed -e "$esc")|g" \
    -e "s|{{ TENANT_ID }}|$(printf '%s' "$tenant_id" | sed -e "$esc")|g" \
    -e "s|{{ TARGET_DIR }}|$(printf '%s' "$target_dir" | sed -e "$esc")|g" \
    "$template" > "$output"
  if command -v python3 >/dev/null 2>&1 && \
     ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$output" 2>/dev/null; then
    log "WARN: settings.json.j2 render: ${output} failed JSON validation; continuing"
    return 0
  fi
  log "settings.json rendered → ${output} (lab=${lab_id:-<empty>}, tenant=${tenant_id:-<empty>}, target=${target_dir:-<empty>})"
}

# NOTE — git credential + identity pre-wire moved to BUILD TIME
# (rockie-workspace#575 rework). It previously lived here as an
# entrypoint-time shell function, but the ENTRYPOINT runs AFTER the
# `USER runtime` directive in Dockerfile.multitenant, so this script
# executes as the unprivileged `runtime` user — which cannot
# `git config --system` (root-owned /etc/gitconfig → "Permission denied").
#
# The `--system` scope is required: the broker `/spawn` + `/ws` PTY spawn
# the agent shell as **root**, which reads /etc/gitconfig, never the
# `runtime` user's /home/runtime/.gitconfig (the --global scope). So the
# credential helpers (github.com + gist.github.com via `gh auth
# git-credential`, huggingface.co via git-credential-hf-env.sh) and the
# default identity are registered by Dockerfile.multitenant RUN steps that
# run as root *before* `USER runtime`. The helpers read GH_TOKEN / HF_TOKEN
# at git-invocation time and emit nothing when their token is absent, so a
# BYOK / open-weights tenant is unaffected. See the credential-helper block
# in Dockerfile.multitenant.

# Warm the subscription binary at machine start so the first user-facing
# call isn't a cold-start (#1222 S4). Runs ONE `--version` in the
# background, best-effort: never blocks broker startup, never fails the
# container. DISABLE_AUTOUPDATER / CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
# are baked into the image (Dockerfile.multitenant), so this warm call
# cannot trigger a self-update or hang on a version ping. A short timeout
# guards against a wedged binary. Version is owned CENTRALLY via the image,
# never self-updated per machine.
warm_subscription_binary() {
  # Pick the binary the tenant actually uses; default to claude.
  local bin="${BINARY:-claude}"
  case "$bin" in
    claude|codex) : ;;
    *) bin="claude" ;;
  esac
  if ! command -v "$bin" >/dev/null 2>&1; then
    log "warm: ${bin} not on PATH; skipping warm-up"
    return 0
  fi
  # Belt-and-suspenders: export the autoupdater-off vars in case the image
  # ENV was overridden by per-machine Fly env, so the warm call can never
  # itself fire an update.
  export DISABLE_AUTOUPDATER="${DISABLE_AUTOUPDATER:-1}"
  export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"
  log "warm: warming ${bin} --version in background (autoupdater disabled)"
  (
    if command -v timeout >/dev/null 2>&1; then
      timeout 20 "$bin" --version >/dev/null 2>&1 \
        && log "warm: ${bin} warmed" \
        || log "warm: ${bin} warm-up exited non-zero (non-fatal)"
    else
      "$bin" --version >/dev/null 2>&1 \
        && log "warm: ${bin} warmed" \
        || log "warm: ${bin} warm-up exited non-zero (non-fatal)"
    fi
  ) &
}

# If the caller passed a command (e.g. `docker run image claude --version`),
# just run it. The mode router only kicks in when no command is given.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# --- platform-owned home overlay + settings render (must precede broker) ----
hydrate_platform_home_bundle

# --- nugget (Goose) per-user overlay (must precede broker spawning nugget) ---
hydrate_nugget_overlay

# --- user-authored private skills (Phase A, S2) -----------------------------
# Materialize the tenant's web-authored skills under ~/.claude/skills so the
# subscription claude/codex binaries and the nugget BYOK harness load them. Best-effort
# and idempotent; the per-session SessionStart hook (settings.json.j2) re-runs
# this so skills authored after boot appear without a Fly restart.
if [ -x /usr/local/bin/sync-user-skills.sh ]; then
  /usr/local/bin/sync-user-skills.sh || log "WARN: initial user-skill sync failed (non-fatal)"
else
  log "WARN: /usr/local/bin/sync-user-skills.sh not present; user skills will not load."
fi

render_settings_json

# --- nugget BYOK provider env -----------------------------------------------
# Must run BEFORE the broker starts: the broker captures its PID-1 env when
# it spawns nugget (apps/broker/owned_env.go forwards the GOOSE_* / provider
# names), so the mapping has to be exported into this process first. The
# legacy byok / open-weights names route here too: the OpenClaw gateway is
# gone, so every BYOK tenant is now served by the broker spawning nugget.
# subscription mode never invokes this and stays byte-identical.
case "$MODE" in
  nugget_byok|byok|open-weights)
    wire_nugget_byok_env
    ;;
  nugget_served)
    # Served runtime: map the deploy-provided SERVED_MODEL_* env onto the
    # Goose provider env and wire identity + output-scrub (all from env).
    # A misconfigured served container (any required value unset) must fail
    # hard rather than start with a leaky fallback.
    if ! wire_nugget_served_env; then
      log "ERROR: nugget_served wiring failed; refusing to start."
      exit 64
    fi
    ;;
esac

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

# --- rockie-loop daemon (MVP step 9) ---------------------------------------
# Continuous autoresearch loop. Pops queued experiments, polls in-flight
# jobs, plans new candidates on idle. Lives at /opt/rockie-loop and runs
# in the background so the broker stays the foreground process Fly
# tracks for liveness.
if [ -x /usr/local/bin/rockie-loop ] && [ -n "${ROCKIELAB_TENANT_TOKEN:-}" ]; then
  log "rockie-loop: starting (api=${ROCKIELAB_API_BASE}, mode=${MODE})"
  /usr/local/bin/rockie-loop run >> /tmp/rockie-loop.log 2>&1 &
  LOOP_PID=$!
  log "rockie-loop: pid=${LOOP_PID}"
elif [ ! -x /usr/local/bin/rockie-loop ]; then
  log "WARN: /usr/local/bin/rockie-loop not present; autoresearch loop disabled."
  LOOP_PID=
else
  log "WARN: ROCKIELAB_TENANT_TOKEN unset; skipping rockie-loop (would 401)."
  LOOP_PID=
fi

case "$MODE" in
  subscription)
    log "MODE=subscription; tenant uses official claude/codex CLIs via OAuth."
    log "Available binaries: $(command -v claude || echo 'claude MISSING') / $(command -v codex || echo 'codex MISSING')"
    # Warm the chosen binary so the first user-facing spawn isn't cold (#1222 S4).
    warm_subscription_binary
    # The broker is the only foreground process; wait -n so SIGTERM kills it.
    if [ -n "${BROKER_PID:-}" ]; then
      wait -n "$BROKER_PID"
    else
      exec tail -f /dev/null
    fi
    ;;
  nugget_byok|byok|open-weights)
    # nugget BYOK mode: the broker spawns the nugget (Goose) binary against
    # the tenant's API key. wire_nugget_byok_env (run before the broker
    # started) translated the platform BYOK contract (BYOK_PROVIDER /
    # BYOK_MODEL_ID + the provider key) into Goose's provider env
    # (GOOSE_PROVIDER / GOOSE_MODEL + OPENAI_BASE_URL/OPENAI_API_KEY or
    # ANTHROPIC_API_KEY) and exported it into PID-1; owned_env.go forwards
    # those names to the spawned nugget. A chat turn arrives via the broker
    # spawning the nugget binary at /usr/local/bin/nugget.
    #
    # The legacy byok / open-weights names land here too: the OpenClaw
    # gateway (the old BYOK engine) is GONE from this thin image, so every
    # BYOK tenant — including ones still pinned on MODE=byok — is served by
    # nugget. This is the generic any-provider mechanism: no provider-
    # specific model values, no masking (a later wave). The broker stays the
    # only foreground process.
    log "MODE=${MODE}; nugget BYOK engine wired (provider=${GOOSE_PROVIDER:-<none>}). Broker-only foreground; no OpenClaw gateway in this image."
    if [ -n "${BROKER_PID:-}" ]; then
      wait -n "$BROKER_PID"
    else
      exec tail -f /dev/null
    fi
    ;;
  nugget_served)
    # Served runtime: the broker spawns the same nugget (Goose) binary, but
    # against a provider endpoint configured entirely from deploy-time env.
    # wire_nugget_served_env (run before the broker started) mapped the
    # SERVED_MODEL_* contract onto Goose's provider env (GOOSE_PROVIDER /
    # GOOSE_MODEL + OPENAI_BASE_URL/OPENAI_API_KEY) and wired the env-provided
    # identity instruction + output-scrub list; owned_env.go forwards those
    # names to the spawned nugget. The broker stays the only foreground
    # process. Endpoint/model/identity/scrub VALUES live only in the
    # environment — never in this image.
    log "MODE=${MODE}; nugget served engine wired (provider=${GOOSE_PROVIDER:-<none>}). Broker-only foreground; no OpenClaw gateway in this image."
    if [ -n "${BROKER_PID:-}" ]; then
      wait -n "$BROKER_PID"
    else
      exec tail -f /dev/null
    fi
    ;;
  *)
    log "ERROR: unknown MODE=${MODE}; expected one of: subscription, nugget_byok, nugget_served, byok, open-weights"
    exit 64
    ;;
esac
