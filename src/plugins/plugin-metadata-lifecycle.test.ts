import { afterEach, describe, expect, it, vi } from "vitest";
import {
  getCurrentPluginMetadataSnapshot,
  setCurrentPluginMetadataSnapshot,
} from "./current-plugin-metadata-snapshot.js";
import { clearCurrentPluginMetadataSnapshotState } from "./current-plugin-metadata-state.js";
import { resolveInstalledPluginIndexPolicyHash } from "./installed-plugin-index-policy.js";
import {
  clearPluginMetadataLifecycleCaches,
  registerPluginMetadataProcessMemoLifecycleClear,
} from "./plugin-metadata-lifecycle.js";
import type { PluginMetadataSnapshot } from "./plugin-metadata-snapshot.js";

let disposeMemoClearer: (() => void) | undefined;

afterEach(() => {
  disposeMemoClearer?.();
  disposeMemoClearer = undefined;
  clearCurrentPluginMetadataSnapshotState();
});

function createSnapshot(): PluginMetadataSnapshot {
  const policyHash = resolveInstalledPluginIndexPolicyHash();
  return {
    policyHash,
    index: {
      version: 1,
      hostContractVersion: "test",
      compatRegistryVersion: "test",
      migrationVersion: 1,
      policyHash,
      generatedAtMs: 1,
      installRecords: {},
      plugins: [],
      diagnostics: [],
    },
    registryDiagnostics: [],
    manifestRegistry: { plugins: [], diagnostics: [] },
    plugins: [],
    diagnostics: [],
    byPluginId: new Map(),
    normalizePluginId: (pluginId) => pluginId,
    owners: {
      channels: new Map(),
      channelConfigs: new Map(),
      providers: new Map(),
      modelCatalogProviders: new Map(),
      cliBackends: new Map(),
      setupProviders: new Map(),
      commandAliases: new Map(),
      contracts: new Map(),
    },
    metrics: {
      registrySnapshotMs: 0,
      manifestRegistryMs: 0,
      ownerMapsMs: 0,
      totalMs: 0,
      indexPluginCount: 0,
      manifestPluginCount: 0,
    },
  };
}

describe("plugin metadata lifecycle caches", () => {
  it("clears the current snapshot and fans out to the registered process memo clearer", () => {
    const clearer = vi.fn();
    setCurrentPluginMetadataSnapshot(createSnapshot());
    disposeMemoClearer = registerPluginMetadataProcessMemoLifecycleClear(clearer);

    clearPluginMetadataLifecycleCaches();

    expect(getCurrentPluginMetadataSnapshot()).toBeUndefined();
    expect(clearer).toHaveBeenCalledTimes(1);
  });

  it("stops calling the registered process memo clearer after disposal", () => {
    const clearer = vi.fn();
    disposeMemoClearer = registerPluginMetadataProcessMemoLifecycleClear(clearer);
    disposeMemoClearer();
    disposeMemoClearer = undefined;

    clearPluginMetadataLifecycleCaches();

    expect(clearer).not.toHaveBeenCalled();
  });

  it("does not let an old disposer unregister a newer process memo clearer", () => {
    const first = vi.fn();
    const second = vi.fn();
    const disposeFirst = registerPluginMetadataProcessMemoLifecycleClear(first);
    disposeMemoClearer = registerPluginMetadataProcessMemoLifecycleClear(second);

    disposeFirst();
    clearPluginMetadataLifecycleCaches();

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
