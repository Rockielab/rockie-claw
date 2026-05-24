# Multi-tenant runtime image

The `rockielab-runtime-multitenant` image is the per-tenant Fly machine
artifact for Rockielab / Pebble ML. One image, three behaviors selected
at boot via `MODE`.

## Modes

| `MODE`         | What runs                                                                                                                            | When to use                                                                               |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| `subscription` | Container stays alive; tenant uses the official `claude` (and `codex`) CLIs against their Pro/Max OAuth session via PTY broker / SSH | Tenants on Anthropic Pro/Max — Anthropic eats LLM cost; platform charges only for compute |
| `byok`         | OpenClaw gateway on `:3000` against the tenant's Anthropic / OpenAI API key                                                          | Tenants that bring their own API key                                                      |
| `open-weights` | OpenClaw gateway on `:3000` pointed at a platform-hosted open-weights endpoint (cerebras / chutes / etc.)                            | Internal, free-tier, or open-source-only tenants                                          |

The image bundles:

- The OpenClaw gateway build (same multi-stage pipeline as the existing
  `Dockerfile`; opted-in extensions controlled by `OPENCLAW_EXTENSIONS`).
- The official Anthropic `@anthropic-ai/claude-code` CLI (binary name `claude`).
- The official OpenAI `@openai/codex` CLI (binary name `codex`).
- Python 3 + pip (skills written in Python need it; the Debian bookworm-slim
  base ships 3.11 — see "Open questions" if you really need 3.12).
- `git`, `curl`, `jq`, `ssh-client`, `tmux`, `rsync`, basic build tools.
- The `platform-skills` repo translated into the official on-disk layout
  Claude Code reads from: `~/.claude/skills/<name>/SKILL.md`,
  `~/.claude/commands/<name>.md`, plus a parallel `~/.codex/` overlay.

## Building

```bash
cd /Users/samuellarson/rocky/platform-runtime
bash scripts/build-multitenant.sh
```

The script auto-locates `platform-skills` (sibling dir, then
`/Users/samuellarson/rocky/platform-skills`). Override with
`PLATFORM_SKILLS_DIR=/path/to/checkout`.

It uses `docker build --build-context skills=...` so the platform-skills
checkout is mounted as a build context rather than copied into the main
context (which would balloon the build payload). The Dockerfile's
`skills-assembly` stage then translates platform-skills' on-disk layout
into the `~/.claude/` layout the official CLI reads.

Image tag defaults to `rockielab-runtime-multitenant:dev`.

## Running locally

### Subscription mode

```bash
docker run --rm -e MODE=subscription rockielab-runtime-multitenant:dev
```

Container will sleep on `tail -f /dev/null`. Exec in to drive the CLI
manually until the Phase 2 PTY broker lands:

```bash
docker exec -it $(docker ps --latest --quiet) claude --version
docker exec -it $(docker ps --latest --quiet) codex --version
```

### BYOK mode

```bash
docker run --rm -p 3000:3000 \
  -e MODE=byok \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  rockielab-runtime-multitenant:dev
```

OpenClaw gateway listens on `:3000`. Health: `curl localhost:3000/healthz`.

### Open-weights mode

```bash
docker run --rm -p 3000:3000 \
  -e MODE=open-weights \
  -e CEREBRAS_BASE_URL=... \
  -e CEREBRAS_API_KEY=... \
  rockielab-runtime-multitenant:dev
```

Tenant config (which provider, which model) is supplied via env. The
gateway's extensions ship in the image (`OPENCLAW_EXTENSIONS=anthropic
codex cerebras chutes` by default).

### One-off CLI checks

Any extra args after the image name override the entrypoint's mode router:

```bash
docker run --rm rockielab-runtime-multitenant:dev claude --version
docker run --rm rockielab-runtime-multitenant:dev codex --version
docker run --rm rockielab-runtime-multitenant:dev ls /home/runtime/.claude/skills
```

## On-disk layout (inside the image)

```
/home/runtime/
├── .claude/
│   ├── settings.json           # tenant-rendered at boot from settings.json.j2
│   ├── settings.json.j2        # template (TENANT_ID / LAB_ID / TARGET_DIR)
│   ├── skills/<name>/SKILL.md  # one per platform-skills skill
│   ├── commands/<name>.md      # slash-command stubs delegating to skills
│   ├── hooks/                  # platform-skills/hooks/ verbatim
│   ├── platform-memory/        # platform-skills/memory/ (init_db.sh + schema)
│   ├── platform-templates/     # platform-skills/templates/
│   ├── platform-scripts/       # platform-skills/scripts/
│   └── platform-docs/          # platform-skills/docs/
├── .codex/
│   ├── skills/                 # mirror of .claude/skills/
│   └── commands/               # mirror of .claude/commands/
└── workspace/                  # tenant working dir (volume mount target)

/app/                           # OpenClaw gateway
├── dist/index.js               # entrypoint shim → openclaw.mjs
├── openclaw.mjs                # canonical CLI
├── extensions/                 # bundled providers (anthropic codex cerebras chutes)
└── node_modules/
```

## Phase boundaries

| Phase    | What lives where                                                                |
| -------- | ------------------------------------------------------------------------------- |
| 1 (this) | Image build + mode router. No PTY broker, no orchestration.                     |
| 2        | PTY broker so `MODE=subscription` exposes claude/codex over a network protocol. |
| 3+       | platform-context drives Fly machine lifecycle and `MODE` selection per tenant.  |

## Open questions

- The bookworm-slim base ships Python 3.11. The spec asks for 3.12.
  Skills that need 3.12 should declare a `uv`/`pyenv`/`venv` at runtime
  rather than bake another interpreter into the image (image bloat).
- Confirm the `@openai/codex` package name when you wake up — the openai/codex
  GitHub repo names this package, but if upstream renames it the build will
  fail at the `npm install -g` step. Build script currently hard-fails
  rather than silently skipping codex.
- The `subscription` mode entrypoint just sleeps. Phase 2 should add a
  PTY broker (e.g. `gotty`, `ttyd`, or a custom acp-broker) that exposes
  claude/codex stdio over a websocket the platform-context can drive.
