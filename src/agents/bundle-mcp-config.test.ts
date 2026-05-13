import { describe, expect, it, vi } from "vitest";
import { loadMergedBundleMcpConfig, toCliBundleMcpServerConfig } from "./bundle-mcp-config.js";

const mocks = vi.hoisted(() => ({
  bundleMcp: {
    config: {
      mcpServers: {
        bundleProbe: {
          command: "node",
          args: ["./servers/probe.mjs"],
        },
      },
    },
    diagnostics: [],
  },
}));

vi.mock("../plugins/bundle-mcp.js", () => ({
  loadEnabledBundleMcpConfig: () => mocks.bundleMcp,
}));

describe("loadMergedBundleMcpConfig", () => {
  it("lets OpenClaw mcp.servers override bundle defaults while preserving raw transport shape", () => {
    const merged = loadMergedBundleMcpConfig({
      workspaceDir: "/workspace",
      cfg: {
        plugins: {
          entries: {
            "bundle-probe": { enabled: true },
          },
        },
        mcp: {
          servers: {
            bundleProbe: {
              transport: "streamable-http",
              url: "https://mcp.example.com/mcp",
            },
          },
        },
      },
    });

    expect(merged.config.mcpServers.bundleProbe).toEqual({
      transport: "streamable-http",
      url: "https://mcp.example.com/mcp",
    });
  });

  it("maps OpenClaw transports to downstream CLI types when requested", () => {
    expect(
      toCliBundleMcpServerConfig({
        transport: "streamable-http",
        url: "https://mcp.example.com/mcp",
      }),
    ).toEqual({
      type: "http",
      url: "https://mcp.example.com/mcp",
    });
    expect(toCliBundleMcpServerConfig({ type: "sse", transport: "streamable-http" })).toEqual({
      type: "sse",
    });
  });

  it("merges a stdio mcp.servers.rockie entry into the bundle catalog (fleet-task #24 BYOK wiring)", () => {
    // Exercises the exact shape that overlay/multitenant/entrypoint.sh
    // writes into ~/.openclaw/openclaw.json on a BYOK tenant: a
    // single stdio MCP server named `rockie` pointing at the same
    // mcp-rockie binary that subscription paths register, with the
    // ROCKIELAB_* env triple mcp-rockie needs to authenticate against
    // platform-context. If OpenClaw upstream renames `mcp.servers`,
    // this assertion catches it before BYOK tenants silently lose
    // their tools.
    const merged = loadMergedBundleMcpConfig({
      workspaceDir: "/workspace",
      cfg: {
        mcp: {
          servers: {
            rockie: {
              command: "node",
              args: ["/home/runtime/mcp-rockie/server.js"],
              env: {
                ROCKIELAB_API_BASE: "https://api.dev.rockielab.com",
                ROCKIELAB_TENANT_DEV_TOKEN: "t-95a34ff7c78c",
                ROCKIELAB_API_PASSWORD: "",
              },
            },
          },
        },
      },
    });

    expect(merged.config.mcpServers.rockie).toEqual({
      command: "node",
      args: ["/home/runtime/mcp-rockie/server.js"],
      env: {
        ROCKIELAB_API_BASE: "https://api.dev.rockielab.com",
        ROCKIELAB_TENANT_DEV_TOKEN: "t-95a34ff7c78c",
        ROCKIELAB_API_PASSWORD: "",
      },
    });
    // Bundle defaults remain present alongside the configured server —
    // configured servers extend, they don't replace.
    expect(merged.config.mcpServers.bundleProbe).toBeDefined();
  });
});
