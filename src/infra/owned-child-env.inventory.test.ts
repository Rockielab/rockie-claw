import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

function readSource(relPath: string): string {
  return fs.readFileSync(path.join(repoRoot, relPath), "utf8");
}

describe("owned child process env inventory", () => {
  it("keeps runtime-owned spawn paths off inherited process.env", () => {
    const runtimeOwnedFiles = [
      "src/agents/bash-tools.exec.ts",
      "src/agents/bash-tools.exec-runtime.ts",
      "src/agents/sandbox/ssh.ts",
      "src/agents/sandbox/ssh-backend.ts",
      "src/auto-reply/reply/stage-sandbox-media.ts",
      "src/process/exec.ts",
      "src/process/supervisor/adapters/child.ts",
      "src/process/supervisor/adapters/pty.ts",
      "src/process/supervisor/supervisor.ts",
    ];

    for (const relPath of runtimeOwnedFiles) {
      const source = readSource(relPath);
      expect(source, relPath).not.toMatch(/env:\s*process\.env\b/);
      expect(source, relPath).not.toMatch(/\?\?\s*process\.env\b/);
      expect(source, relPath).not.toMatch(/\.\.\.\s*process\.env\b/);
    }
  });

  it("covers the known raw spawn wrappers with explicit owned env guards", () => {
    const expectations = [
      {
        relPath: "src/agents/sandbox/ssh.ts",
        required: [
          "sanitizeEnvVars(buildOwnedChildEnv()).allowed",
          "env: buildOwnedChildEnv()",
          "assertNoSecretValuesInArgv",
        ],
      },
      {
        relPath: "src/auto-reply/reply/stage-sandbox-media.ts",
        required: ["env: buildOwnedChildEnv()"],
      },
      {
        relPath: "src/process/supervisor/adapters/child.ts",
        required: ["assertOwnedChildEnv(params.env", "prepareOomScoreAdjustedSpawn"],
      },
      {
        relPath: "src/process/supervisor/adapters/pty.ts",
        required: ["assertOwnedChildEnv(params.env", "prepareOomScoreAdjustedSpawn"],
      },
      {
        relPath: "src/agents/bash-tools.exec-runtime.ts",
        required: ["buildOwnedChildEnv()"],
      },
      {
        relPath: "src/process/exec.ts",
        required: ["buildOwnedChildEnv()"],
      },
    ];

    for (const expectation of expectations) {
      const source = readSource(expectation.relPath);
      for (const needle of expectation.required) {
        expect(source, expectation.relPath).toContain(needle);
      }
    }
  });
});
