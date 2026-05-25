# Upstream Delta bb6f37: Plugin Metadata Lifecycle Cache Cleanup

## Decision Summary

Port the lifecycle-cache abstraction from upstream `e7c696a5b0` without porting the broader metadata snapshot process-memo performance series. Rockie already clears the Gateway-owned current plugin metadata snapshot at the two required lifecycle boundaries, so the useful delta is a small fanout helper that preserves current behavior and gives a future memo layer one lifecycle hook.

Implement `clearPluginMetadataLifecycleCaches()` as the single lifecycle invalidation entry point. It should clear the existing current snapshot state and then invoke an optionally registered process-memo clearer when one exists. Do not add upstream's `loadPluginMetadataSnapshot` memo implementation now.

## Files Touched

Expected implementation files:

- `src/plugins/plugin-metadata-lifecycle.ts`: new helper and optional registration API.
- `src/gateway/server.impl.ts`: switch close prelude from `clearCurrentPluginMetadataSnapshot()` to `clearPluginMetadataLifecycleCaches()`.
- `src/plugins/installed-plugin-index-store.ts`: switch persisted installed-index write invalidation from `clearCurrentPluginMetadataSnapshotState()` to `clearPluginMetadataLifecycleCaches()`.
- `src/plugins/current-plugin-metadata-snapshot.test.ts`: keep existing current-snapshot behavior coverage; adjust imports only if needed.
- `src/plugins/plugin-metadata-lifecycle.test.ts`: add focused fanout coverage.
- `src/plugins/installed-plugin-index-store.test.ts`: add lifecycle-boundary callsite coverage for successful persisted-index writes.
- `src/gateway/server-import-boundary.test.ts`: add focused Gateway close-prelude coverage for lifecycle cache clearing.

This design file is the only file changed by the Plan role: `designs/upstream-platform-runtime-delta-bb6f37-2026-05-25.md`.

## Exact Implementation Steps

1. Add `src/plugins/plugin-metadata-lifecycle.ts`:
   - Import `clearCurrentPluginMetadataSnapshotState` from `src/plugins/current-plugin-metadata-state.ts`.
   - Hold one module-local optional callback: `let clearPluginMetadataProcessMemo: (() => void) | undefined;`.
   - Export `registerPluginMetadataProcessMemoLifecycleClear(clearProcessMemo: () => void): () => void`, assigning that callback and returning a disposer.
   - The disposer must unregister only when the currently registered callback is the same callback it registered, so overlapping tests or future replacement registration cannot accidentally clear a newer callback.
   - Export `clearPluginMetadataLifecycleCaches(): void`, calling `clearCurrentPluginMetadataSnapshotState()` first and `clearPluginMetadataProcessMemo?.()` second.

2. Update `src/gateway/server.impl.ts`:
   - Replace the `clearCurrentPluginMetadataSnapshot` import with `clearPluginMetadataLifecycleCaches`.
   - In `runClosePrelude`, call `clearPluginMetadataLifecycleCaches()` immediately after `markClosePreludeStarted()`.
   - Leave Rockie Gateway shutdown behavior otherwise unchanged.

3. Update `src/plugins/installed-plugin-index-store.ts`:
   - Replace the direct `clearCurrentPluginMetadataSnapshotState` import with `clearPluginMetadataLifecycleCaches`.
   - In both `writePersistedInstalledPluginIndex` and `writePersistedInstalledPluginIndexSync`, call `clearPluginMetadataLifecycleCaches()` after the index write succeeds.
   - Preserve existing persistence behavior, permissions, JSON shape, warning injection, and refresh paths.

4. Add focused tests in `src/plugins/plugin-metadata-lifecycle.test.ts`:
   - Keep registration local to the test file. Store the disposer returned by `registerPluginMetadataProcessMemoLifecycleClear` and call it in `afterEach`, then also clear the current metadata snapshot state.
   - Seed a current metadata snapshot through `setCurrentPluginMetadataSnapshot`.
   - Register a `vi.fn()` memo clearer with `registerPluginMetadataProcessMemoLifecycleClear`.
   - Call `clearPluginMetadataLifecycleCaches()`.
   - Assert `getCurrentPluginMetadataSnapshot()` is `undefined`.
   - Assert the registered clearer was called once.
   - Add a disposer case proving that after disposal, `clearPluginMetadataLifecycleCaches()` no longer calls the old memo clearer.
   - Add a replacement/overlap disposer case proving identity-guarded disposal:
     register `first`, register `second`, call the first disposer, then call `clearPluginMetadataLifecycleCaches()`. Assert `first` was not called and `second` was called once. This is required because the disposer must not clear a newer registration it did not create.

5. Add lifecycle-boundary callsite coverage in `src/plugins/installed-plugin-index-store.test.ts`:
   - Register a `vi.fn()` memo clearer and dispose it in `afterEach`.
   - Trigger a real successful persisted-index write through `writePersistedInstalledPluginIndex` using the existing temp state-dir helpers. Assert the clearer was not called before the write, and was called once after the awaited write returns successfully.
   - Trigger a separate real successful persisted-index write through `writePersistedInstalledPluginIndexSync` using the same style of temp state-dir helper. Assert the clearer was not called before the write, and was called once after the sync write returns successfully.
   - Cover both public write paths because they have separate lifecycle-clear callsites. Do not collapse this into only testing `clearPluginMetadataLifecycleCaches()` directly.

6. Add Gateway close coverage in `src/gateway/server-import-boundary.test.ts`:
   - Add a focused executable test for the close prelude rather than relying on a one-off static verification command.
   - Mock `src/plugins/plugin-metadata-lifecycle.ts` so `clearPluginMetadataLifecycleCaches()` records a call, and mock the close-prelude dependency loaded by `runClosePrelude` so the test can observe the sequence without running heavy shutdown hooks.
   - Exercise the real Gateway close path that invokes `runClosePrelude`, then assert `clearPluginMetadataLifecycleCaches()` was called once before the mocked close-prelude hook work runs.
   - Keep or extend the existing lightweight source-order assertion only as secondary coverage that `markClosePreludeStarted();` appears before `clearPluginMetadataLifecycleCaches();` in `runClosePrelude`. Do not list static parsing as the primary proof.

7. Do not import or register anything from `src/plugins/plugin-metadata-snapshot.ts` until Rockie deliberately adds a process memo. The registration API exists for that future change only.

## Test / Dogfood Plan

Targeted local proof:

- `pnpm test src/plugins/plugin-metadata-lifecycle.test.ts src/plugins/current-plugin-metadata-snapshot.test.ts src/plugins/installed-plugin-index-store.test.ts`
- `pnpm test src/gateway/server-import-boundary.test.ts`
- `pnpm exec oxfmt --check --threads=1 src/plugins/plugin-metadata-lifecycle.ts src/plugins/plugin-metadata-lifecycle.test.ts src/plugins/installed-plugin-index-store.ts src/gateway/server.impl.ts`
- Gateway close prelude checkpoint: the required proof is the focused executable test above. Static source-order parsing may remain only as a small supporting assertion inside `src/gateway/server-import-boundary.test.ts`.

Changed-gate handoff proof:

- Because this is source/test runtime behavior, run `pnpm check:changed` in Testbox by default before handoff/push.
- If changed lanes expand broadly, keep the broad gate on Testbox and use local only for the targeted tests above.

Dogfood:

- No live provider or channel credentials are required.
- Manual runtime smoke is optional: start a Gateway, trigger a normal shutdown/restart, and verify no plugin metadata stale-state warnings appear.

## Risks And Non-Goals

Risks:

- A module-local registered clearer can leak across tests if multiple suites register different callbacks. The registration API must return a disposer, and every test registering a clearer must call that disposer in `afterEach`.
- Directly clearing `current-plugin-metadata-state` remains lower-level than the public `clearCurrentPluginMetadataSnapshot()` API, but this matches upstream's helper and prevents import cycles through `current-plugin-metadata-snapshot.ts`.
- The helper centralizes lifecycle invalidation but does not make metadata caches request-scoped; stale-cache prevention still depends on lifecycle callsites using the helper.

Non-goals:

- Do not add upstream's metadata snapshot process memo, clone helpers, persisted-registry fingerprinting, or `resolvePluginMetadataSnapshot` changes.
- Do not change plugin discovery, manifest registry loading, installed-index schema, or Gateway startup plugin selection.
- Do not change Rockie's multitenant entrypoint, broker, Docker image, platform skills overlay, or subscription/BYOK/open-weights routing.
- Do not update `scripts/test-projects.test-support.mjs`.

## Explicit Skipped Upstream Surfaces

Skipped from upstream `e7c696a5b0`:

- `src/plugins/plugin-metadata-snapshot.ts` process memo state and `clearLoadPluginMetadataSnapshotMemo`.
- Memo key/fingerprint helpers for env, persisted registry files, npm root, and installed-index hashes.
- Snapshot clone/deep-copy helpers used to protect memoized values from mutation.
- `resolvePluginMetadataSnapshot` and persisted-registry memo lookup paths.
- Upstream `src/plugins/plugin-metadata-snapshot.memo.test.ts` as a direct port, because Rockie does not have the memo layer it tests.
- Any incidental upstream churn outside the two existing lifecycle callsites and the new lifecycle fanout helper.
