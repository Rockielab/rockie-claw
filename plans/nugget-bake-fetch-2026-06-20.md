# Plan — nugget bake via fetch (supersedes #108)

Repo: github.com/Rockielab/platform-runtime
Branch: fleet/implementer-nugget-bake-fetch-2026-06-20
Pinned rockie-nugget SHA: 13ae0cde60fe8df21f48d44a3727587e82fe3eb9

## Goal
Bake the nugget harness into Dockerfile.multitenant by FETCHING the overlay from
rockie-nugget@<SHA> at build time (single source of truth, no drift), replacing
PR #108's stale committed-snapshot approach. ONE codebase, config-only, local≡platform.

## Constraints
- NO `.github/workflows/**` changes (defaults hardcoded so no workflow build-arg).
- Additive only: claude/codex/openclaw stages byte-unchanged in behavior.
- Masking: generic only; overlay is masking-clean at the pinned SHA.
- No vendored overlay/multitenant/nugget/* committed files.

## Approach
- New `nugget-overlay` build stage:
  - ARG NUGGET_OVERLAY_REF (default = pinned SHA).
  - ARG NUGGET_GOOSE_URL / NUGGET_GOOSE_SHA256 hardcoded defaults (release v1.38.0-glibc236).
  - Fetch repo tarball at REF (public codeload, sha-pin via REF).
  - Fetch + sha-verify goose binary.
  - Run rockie-nugget install.sh with platform target env overrides into a staging
    XDG dir → guarantees local≡platform (same installer).
  - Network-unreachable fallback: stub goose that exits non-zero w/ clear message,
    so claude/codex/openclaw still build.
- Final stage: COPY staged goose + XDG config tree + a platform `nugget` wrapper
  that forwards broker argv to goose (broker spawns literal `nugget run --no-session
  --output-format stream-json -t <prompt>`; entrypoint wire_nugget_byok_env already
  maps BYOK→GOOSE at PID-1, so wrapper is a thin passthrough).
- scripts/build-multitenant.sh: pass the new ARGs through for local builds.

## Milestones
- [ ] M1: Dockerfile.multitenant nugget-overlay stage + final bake (additive).
- [ ] M2: scripts/build-multitenant.sh ARG passthrough.
- [ ] M3: Masking scan clean; commit.
- [ ] M4: Hetzner build + 3-mode dogfood (nugget_byok CURRENT overlay; claude/codex non-regress).
- [ ] M5: Push + open PR (no merge). Supersedes #108.

## Log
- [HEARTBEAT] 2026-06-20 plan written; inputs verified (public tarball+release fetch OK, broker execs literal `nugget`, entrypoint wires BYOK→GOOSE).
- [HEARTBEAT] M1+M2+M3 done: Dockerfile nugget-overlay stage + final bake + entrypoint hydration + build script ARGs; committed 8ce718b8; pushed; masking clean.
- [HEARTBEAT] M4 in progress: Hetzner nugget-overlay stage builds GREEN (config.yaml has /opt/nugget/src paths + enabled:true, .goosehints+recipes+memory+hooks staged). Found upstream install.sh trailing `set -u` MCP_DIR bug — bake tolerates it + verifies artifacts.
- [HEARTBEAT] M4 DONE. nugget_byok PROVEN on Hetzner: focused image (real nugget-overlay stage + real broker), goose 1.38.0 runs on glibc236 (#108 blocker GONE), MODE=nugget_byok turn via BYOK OpenAI-compatible localhost endpoint → 36 frames, goose called research-env-v1__run_command (CURRENT overlay) → faithful combined output NUGGET_BAKE_OK + 161 + [exit 0]. MCP tools/list = 9 REAL tools; fetch_url SSRF guard REFUSED 169.254.169.254 (not a stub). Full multitenant image blocked by PRE-EXISTING openclaw `pnpm build:docker` rolldown bug (resolveFfmpegBin) — reproduced identically from pristine main → not my change. Diff purely additive: apps/broker UNTOUCHED, claude/codex/openclaw stages unchanged. Masking clean.
- [HEARTBEAT] M5: pushing + opening PR (supersedes #108).
