#!/usr/bin/env node
// Trip if a future change re-adds `--config` to overlay/tenant/start.sh.
//
// The `openclaw gateway` CLI does NOT declare `--config`. The tenant flow
// renders its config to $TENANT_HOME/openclaw.json (a non-default path) and
// MUST deliver it via the OPENCLAW_CONFIG_PATH env var instead. See
// rockie-workspace#60.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const TARGET = resolve(HERE, "..", "overlay", "tenant", "start.sh");

function main() {
  const src = readFileSync(TARGET, "utf8");
  // Strip comment-only lines (leading whitespace + `#`) so the rationale
  // comment in start.sh that names the dropped `--config` flag is not a
  // false positive. We still catch `--config` on any code line, including
  // inline-trailing-comment cases like `exec openclaw ... --config foo # x`.
  const codeOnly = src
    .split("\n")
    .filter((line) => !/^\s*#/.test(line))
    .join("\n");
  if (!codeOnly.includes("--config")) {
    return 0;
  }
  console.error(
    [
      `check-tenant-config-path: \`--config\` reappeared in ${TARGET}.`,
      "",
      "The `openclaw gateway` CLI does not declare a --config option;",
      "passing it relies on Commander's allowUnknownOption and will start",
      "erroring the moment that changes. Deliver the rendered config path",
      "via `export OPENCLAW_CONFIG_PATH=\"$RENDERED\"` before the exec,",
      "mirroring the cascade-#4 pattern in overlay/multitenant/entrypoint.sh.",
      "",
      "Refs: rockie-workspace#60",
    ].join("\n"),
  );
  return 1;
}

process.exitCode = main();
