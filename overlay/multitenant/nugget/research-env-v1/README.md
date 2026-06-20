# research-env-v1 — the research agent's action space

This bundle is the **versioned tool menu** the Rockie research agent may call. It is
the human-readable companion to the machine-readable JSON Schemas in `tools/` and the
`manifest.json` index.

**Not a freeze.** Per the spec (§3.1, A2/C2), this version is _iterated freely_ — adding
or changing a tool cuts a new version (`research-env-v2`), it does not edit v1 in place.
The byte-identical-freeze discipline is a future RL concern, not active now. The only
live rule: **within one eval/experiment run, one version is used end-to-end.** See
`../VERSIONING.md`.

## The nine tools

| Tool          | Verb                    | Input (required)               | Returns                                            |
| ------------- | ----------------------- | ------------------------------ | -------------------------------------------------- |
| `web_search`  | gather from the web     | `query`                        | ranked title/URL/snippet list                      |
| `fetch_url`   | read one source         | `url`                          | extracted main-content text                        |
| `read_file`   | workspace read          | `path`                         | file text                                          |
| `list_files`  | workspace listing       | — (`path` defaults `.`)        | one workspace-relative path per line               |
| `write_file`  | workspace write         | `path`, `content`              | confirmation (path + bytes written)                |
| `run_command` | execute (CPU sandbox)   | `command`                      | combined stdout+stderr + exit code                 |
| `submit_job`  | run a GPU experiment    | `hardware`, `image`, `command` | opaque job `handle`                                |
| `get_job`     | get experiment result   | `handle`                       | normalized state / metrics / logs / artifact paths |
| `finish`      | terminate with a result | `result`                       | (terminal; ends the loop)                          |

This is the §3.2 starting set — sufficient for the north-star task (the agent improves
Rockie's own research stack: read code → write code → launch a training run → read
metrics → iterate). It is a _starting_ set, not a final one.

## Result-string format (train/serve symmetry surface)

The policy only ever sees **tokens**: the tool definitions above and the **result string**
each call returns. So the result string is part of the contract and is documented here.

- Every tool result arrives as a **single text content block**.
- **Success** is either raw text (e.g. `read_file`, `run_command` output) or
  **pretty-printed JSON** (2-space indent) for structured results (e.g. `get_job`,
  `web_search`). Large structured payloads are truncated to a bounded length.
- **Error** is a JSON object with a stable shape, so the model learns one error grammar:

  ```json
  { "error": { "code": "<stable_string>", "message": "<human-readable>" } }
  ```

  `code` is a stable machine token (e.g. `unknown_tool`, `tool_error`); `message` is prose.

## Provider-invariance of the GPU tools (§3.3)

`submit_job` / `get_job` are the **invariant surface** for compute. The model expresses a
**capability** (`hardware` class, `image`, `command`) and receives an **opaque handle**;
it never sees — in any field, description, enum, or result string — the provider identity,
region, connection protocol (SSH/API/WS), credentials, instance lifecycle, failover, or
billing. All of that lives **below** the tool boundary and the GPU-arbitrage epic evolves
there invisibly.

Two kinds of variance, opposite treatment (§3.3):

- **Hidden entirely** (incidental plumbing): provider, protocol, auth, region, lifecycle
  quirks, provisioning-latency variance, retries, billing.
- **Surfaced but normalized** (real research variables): hardware class, framework/runtime
  (chosen via `image`), version pins, VRAM, metrics/logs. These genuinely differ and are
  surfaced as a clean, uniform descriptor — never a provider-specific surprise.

Two identical jobs run on different providers must produce **byte-equivalent** model-visible
text except for legitimate research variables. Enforcing that end-to-end is the A-AUDIT /
A6 slices' job; this contract defines the _target_ surface they normalize toward.

## Wiring status

These schemas are the **idealized contract**, not yet wired to the live server. The
live `mcp-rockie` server exposes a different, larger surface (e.g. `experiment_submit`
/ `job_status` instead of `submit_job` / `get_job`, and it still leaks some provider-shaped
fields). `../RECONCILE.md` maps every contract tool to its real counterpart with a
`exists / rename / needs-adding` status, so a later slice can wire the two.
