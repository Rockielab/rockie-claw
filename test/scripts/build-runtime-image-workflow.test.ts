import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { parse } from "yaml";

const WORKFLOW_PATH = ".github/workflows/build-runtime-image.yml";

type WorkflowStep = {
  if?: string;
  name?: string;
  run?: string;
  uses?: string;
  with?: Record<string, string>;
};

type WorkflowJob = {
  env?: Record<string, string>;
  steps?: WorkflowStep[];
};

type Workflow = {
  jobs?: Record<string, WorkflowJob>;
};

function readWorkflow(): Workflow {
  return parse(readFileSync(WORKFLOW_PATH, "utf8")) as Workflow;
}

function rolloutJob(): WorkflowJob {
  const job = readWorkflow().jobs?.["rollout-tenants"];
  expect(job, "expected rollout-tenants job").toBeDefined();
  return job!;
}

function workflowStep(job: WorkflowJob, stepName: string): WorkflowStep {
  const step = job.steps?.find((candidate) => candidate.name === stepName);
  expect(step, `expected workflow step ${stepName}`).toBeDefined();
  return step!;
}

describe("build-runtime-image rollout workflow", () => {
  it("retries transient Cloudflare and connection failures with bounded backoff", () => {
    const rollout = workflowStep(rolloutJob(), "Roll every tenant to the new image SHA");
    const run = rollout.run ?? "";

    expect(run).toContain("ROLLOUT_MAX_ATTEMPTS");
    expect(run).toContain("backoff_seconds=5");
    expect(run).toContain('if [ "$backoff_seconds" -gt 60 ]; then');
    expect(run).toContain("000|520|521|522|523|524");
    expect(run).toContain("curl_exit=$?");
    expect(run).toContain('code="000"');
    expect(run).toContain('sleep "$backoff_seconds"');
  });

  it("writes and uploads a rollout summary artifact with retry metadata", () => {
    const job = rolloutJob();
    expect(job.env).toMatchObject({
      IMAGE_TAG: "ghcr.io/saml212/rockielab-runtime-multitenant:${{ github.sha }}",
      ROLLOUT_ARTIFACT_DIR: ".artifacts/runtime-rollout",
      ROLLOUT_MAX_ATTEMPTS: "5",
    });

    const rollout = workflowStep(job, "Roll every tenant to the new image SHA");
    const run = rollout.run ?? "";
    expect(run).toContain("rollout-summary.md");
    expect(run).toContain("rollout-summary.json");
    expect(run).toContain("attempts.jsonl");
    expect(run).toContain("response_codes");
    expect(run).toContain("retry_count");
    expect(run).toContain("final_result");
    expect(run).toContain("image_sha");
    expect(run).toContain("buckets");
    expect(run).toContain("updated");
    expect(run).toContain("skipped");
    expect(run).toContain("failed");
    expect(run).toContain("total");

    const upload = workflowStep(job, "Upload rollout summary artifact");
    expect(upload.if).toBe("always()");
    expect(upload.uses).toBe("actions/upload-artifact@v4");
    expect(upload.with).toMatchObject({
      name: "runtime-rollout-summary-${{ github.sha }}",
      path: "${{ env.ROLLOUT_ARTIFACT_DIR }}",
      "if-no-files-found": "ignore",
    });
  });

  it("leaves exact failure bodies and a manual single-tenant recovery command", () => {
    const rollout = workflowStep(rolloutJob(), "Roll every tenant to the new image SHA");
    const run = rollout.run ?? "";

    expect(run).toContain("response_body: $body");
    expect(run).toContain("### Final response body");
    expect(run).toContain("### Manual single-tenant recovery");
    expect(run).toContain("/api/tenants/<tenant-id>/image");
    expect(run).toContain("ROCKIELAB_ADMIN_TOKEN");
    expect(run).toContain("ROCKIELAB_API_PASSWORD");
  });
});
