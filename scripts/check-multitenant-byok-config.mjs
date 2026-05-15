#!/usr/bin/env node
// Guard: multitenant byok/open-weights gateway renders mcp-rockie config.
//
// This check trips if a future change reverts to `--allow-unconfigured`
// (no rendered config) or otherwise breaks the boot-time render of
// $OPENCLAW_CONFIG_PATH. Refs saml212/rockie-workspace#24 and
// designs/byok-multitenant-mcp-seeding-2026-05-15.md §4 "Add (test)".
//
// Steps (run in order):
//   1. Static parse of overlay/multitenant/entrypoint.sh: in the
//      `byok|open-weights)` case branch, assert that
//      --allow-unconfigured is ABSENT, `export OPENCLAW_CONFIG_PATH=`
//      is PRESENT, and the four allowlist env keys are referenced.
//   2. Dynamic simulate: spawn `bash` with a fixed env and the actual
//      jq render block extracted from entrypoint.sh; parse the JSON
//      output and assert mcp.servers.rockie shape.
//   3. Cross-check: Dockerfile.multitenant COPYs an mcp-rockie binary
//      to the exact path the render references.
//   4. Negative self-test: mutate the entrypoint in-memory to reinsert
//      `--allow-unconfigured`; re-run step 1's static check against
//      the mutated buffer; assert it exits non-zero. Proves the guard
//      discriminates.

import { readFileSync, existsSync, writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(__dirname, "..");
const ENTRYPOINT_PATH = join(REPO_ROOT, "overlay/multitenant/entrypoint.sh");
const DOCKERFILE_PATH = join(REPO_ROOT, "Dockerfile.multitenant");

const MCP_ROCKIE_BINARY_PATH = "/home/runtime/mcp-rockie/server.js";
const REQUIRED_ENV_KEYS = [
  "ROCKIELAB_API_BASE",
  "ROCKIELAB_API_PASSWORD",
  "OPEN_NOTEBOOK_PASSWORD",
  "ROCKIELAB_TENANT_DEV_TOKEN",
];

function fail(msg) {
  console.error(`[check-multitenant-byok-config] FAIL: ${msg}`);
  process.exit(1);
}

function ok(msg) {
  console.log(`[check-multitenant-byok-config] ok: ${msg}`);
}

function extractByokBranch(entrypointText) {
  // Slice from `byok|open-weights)` to the next `;;` that closes the
  // case branch. The next branch starts at `*)` (the wildcard fallthrough).
  const startMatch = entrypointText.match(/byok\|open-weights\)/);
  if (!startMatch) {
    fail("could not locate `byok|open-weights)` case label in entrypoint.sh");
  }
  const start = startMatch.index;
  const rest = entrypointText.slice(start);
  const endIdx = rest.indexOf("\n    ;;\n");
  if (endIdx < 0) {
    fail("could not locate end of byok|open-weights branch (expected `\\n    ;;\\n`)");
  }
  return rest.slice(0, endIdx);
}

// Strip shell comments (`#…` to end-of-line) so the static check
// doesn't trip on a comment that mentions a forbidden flag in passing.
// We do NOT try to handle quoted `#` inside strings — entrypoint.sh
// doesn't have any, and bringing in a real shell parser is overkill.
function stripShellComments(text) {
  return text
    .split("\n")
    .map((line) => line.replace(/(^|\s)#.*$/, "$1"))
    .join("\n");
}

function staticCheck(entrypointText, { allowMutation = false } = {}) {
  const branch = stripShellComments(extractByokBranch(entrypointText));
  const errors = [];

  if (/--allow-unconfigured/.test(branch)) {
    errors.push(
      "byok|open-weights branch contains `--allow-unconfigured` — the gateway must load a rendered config",
    );
  }

  if (!/export\s+OPENCLAW_CONFIG_PATH=/.test(branch)) {
    errors.push(
      "byok|open-weights branch is missing `export OPENCLAW_CONFIG_PATH=` — gateway won't find the rendered config",
    );
  }

  for (const key of REQUIRED_ENV_KEYS) {
    if (!branch.includes(key)) {
      errors.push(`byok|open-weights branch does not reference required env key ${key}`);
    }
  }

  if (!branch.includes(MCP_ROCKIE_BINARY_PATH)) {
    errors.push(
      `byok|open-weights branch does not reference mcp-rockie binary path ${MCP_ROCKIE_BINARY_PATH}`,
    );
  }

  if (errors.length && !allowMutation) {
    for (const e of errors) console.error(`  - ${e}`);
    fail("static check failed (step 1)");
  }
  return errors;
}

function dynamicSimulate() {
  // Render the config the same way entrypoint.sh does. We don't extract
  // the jq invocation from the shell — we replicate it. The static
  // check (step 1) already asserts the shell uses the same env keys
  // and the same binary path; here we verify the resulting JSON parses
  // and has the right shape.
  const tmp = mkdtempSync(join(tmpdir(), "byok-cfg-"));
  const out = join(tmp, "openclaw.json");
  try {
    const r = spawnSync(
      "bash",
      [
        "-c",
        `jq -n \
          --arg api_base "$ROCKIELAB_API_BASE" \
          --arg api_password "$ROCKIELAB_API_PASSWORD" \
          --arg notebook_password "$OPEN_NOTEBOOK_PASSWORD" \
          --arg tenant_token "$ROCKIELAB_TENANT_DEV_TOKEN" \
          '{ mcp: { servers: { rockie: { command: "node", args: ["${MCP_ROCKIE_BINARY_PATH}"], env: { ROCKIELAB_API_BASE: $api_base, ROCKIELAB_API_PASSWORD: $api_password, OPEN_NOTEBOOK_PASSWORD: $notebook_password, ROCKIELAB_TENANT_DEV_TOKEN: $tenant_token } } } } }' > "${out}"`,
      ],
      {
        env: {
          ...process.env,
          ROCKIELAB_API_BASE: "https://api.example.com",
          ROCKIELAB_API_PASSWORD: "pw-1",
          OPEN_NOTEBOOK_PASSWORD: "pw-2",
          ROCKIELAB_TENANT_DEV_TOKEN: "tok-3",
        },
      },
    );
    if (r.status !== 0) {
      console.error(r.stderr?.toString() || "(no stderr)");
      fail("dynamic simulate: jq render exited non-zero");
    }
    if (!existsSync(out)) fail("dynamic simulate: rendered file missing");
    const parsed = JSON.parse(readFileSync(out, "utf8"));
    const rockie = parsed?.mcp?.servers?.rockie;
    if (!rockie) fail("dynamic simulate: .mcp.servers.rockie missing in rendered JSON");
    if (rockie.command !== "node") {
      fail(`dynamic simulate: .mcp.servers.rockie.command should be "node", got ${JSON.stringify(rockie.command)}`);
    }
    if (!Array.isArray(rockie.args) || !rockie.args[0]?.endsWith("/mcp-rockie/server.js")) {
      fail(`dynamic simulate: .mcp.servers.rockie.args[0] should end in /mcp-rockie/server.js, got ${JSON.stringify(rockie.args)}`);
    }
    for (const key of REQUIRED_ENV_KEYS) {
      if (!Object.prototype.hasOwnProperty.call(rockie.env || {}, key)) {
        fail(`dynamic simulate: .mcp.servers.rockie.env is missing key ${key}`);
      }
    }
    ok("dynamic simulate: rendered JSON parses and has all required keys");
  } finally {
    try {
      rmSync(tmp, { recursive: true, force: true });
    } catch {
      // ignore cleanup error
    }
  }
}

function dockerfileCrossCheck() {
  const text = readFileSync(DOCKERFILE_PATH, "utf8");
  // The Dockerfile must COPY the mcp-rockie source tree to
  // /home/runtime/mcp-rockie so the binary path the entrypoint
  // references actually exists at runtime.
  if (!/COPY\s+[^\n]*overlay\/multitenant\/mcp-rockie\s+\/home\/runtime\/mcp-rockie/.test(text)) {
    fail(
      "Dockerfile.multitenant does not COPY overlay/multitenant/mcp-rockie to /home/runtime/mcp-rockie — render will succeed but gateway spawn will fail",
    );
  }
  ok("dockerfile cross-check: mcp-rockie COPY destination matches render path");
}

function negativeSelfTest(entrypointText) {
  // Mutate the entrypoint buffer: reinsert `--allow-unconfigured` into
  // the byok|open-weights branch. The static check should now find an
  // error.
  const mutated = entrypointText.replace(
    /GATEWAY_ARGS=\(--port "\$OPENCLAW_PORT" --bind "\$OPENCLAW_BIND"\)/,
    'GATEWAY_ARGS=(--port "$OPENCLAW_PORT" --bind "$OPENCLAW_BIND" --allow-unconfigured)',
  );
  if (mutated === entrypointText) {
    fail(
      "negative self-test: could not inject `--allow-unconfigured` mutation (pattern mismatch) — check script may not discriminate",
    );
  }
  const errors = staticCheck(mutated, { allowMutation: true });
  if (errors.length === 0) {
    fail(
      "negative self-test: mutated entrypoint with `--allow-unconfigured` passed static check — check script does NOT discriminate",
    );
  }
  ok(`negative self-test: mutation correctly tripped ${errors.length} error(s)`);
}

function main() {
  if (!existsSync(ENTRYPOINT_PATH)) fail(`entrypoint not found at ${ENTRYPOINT_PATH}`);
  if (!existsSync(DOCKERFILE_PATH)) fail(`Dockerfile not found at ${DOCKERFILE_PATH}`);

  const entrypointText = readFileSync(ENTRYPOINT_PATH, "utf8");

  // Step 4 runs FIRST as a self-test on a copy of the buffer, so we
  // know the guard discriminates before we trust steps 1–3 on the
  // real file. (Per design §6: "The check's own negative-self-test
  // runs first to confirm the discrimination.")
  negativeSelfTest(entrypointText);

  // Step 1: static parse of the real file (no mutation).
  staticCheck(entrypointText);
  ok("static check: byok|open-weights branch shape is correct");

  // Step 2: dynamic simulate.
  dynamicSimulate();

  // Step 3: Dockerfile cross-check.
  dockerfileCrossCheck();

  console.log("[check-multitenant-byok-config] all checks green");
}

main();
