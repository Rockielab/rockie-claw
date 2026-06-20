# Plan — de-OpenClaw the multitenant runtime (thin image)

Branch: `fleet/implementer-deopenclaw-thin-runtime-2026-06-20`
Repo: Rockielab/platform-runtime
Reference: Rockielab/rockie-runtime main (already de-OpenClaw'd)

## Goal
Drop OpenClaw from `Dockerfile.multitenant` so the image is a THIN runtime
(broker + claude + codex + nugget + overlay). Fixes the broken/timed-out
build (OpenClaw `pnpm build:docker` `resolveFfmpegBin` error + 60-min CI
timeout). nugget replaces OpenClaw as the BYOK engine; claude/codex run via
the broker with no OpenClaw.

## Milestones
- [x] M1: Clone platform-runtime + rockie-runtime ref to /tmp; diff.
- [x] M2: Dockerfile.multitenant — remove OpenClaw stages + final COPYs +
      gateway EXPOSE/HEALTHCHECK; keep nugget fetch + broker + claude/codex.
- [x] M3: entrypoint.sh — remove OpenClaw gateway start + openclaw.json
      templating; map legacy byok/open-weights → nugget BYOK; keep
      subscription/nugget_byok byte-identical.
- [x] M4: build-multitenant.sh — drop OPENCLAW_EXTENSIONS build-arg.
- [ ] M5: Hetzner full-image build (green? fast?) + 3-mode dogfood.
- [ ] M6: Push branch + open PR (no merge).

## Constraints
- NO .github/workflows/** changes.
- claude/codex byte-identical (no OpenClaw in their spawn).
- Masking: generic only (no DeepSeek/Stone-1). Scan clean.
- Broker Go `openclaw` harness left untouched (dead-but-harmless; separate
  concern, touching it risks claude/codex spawn).

## Log
[HEARTBEAT] 2026-06-20 M1-M4 done; Dockerfile -114 lines, entrypoint
de-gatewayed, build-arg dropped; masking scan clean. Next: Hetzner build.
