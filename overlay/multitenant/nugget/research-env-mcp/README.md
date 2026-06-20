# research-env-v1 — stdio MCP server (thin shim)

A dependency-free Python (stdlib-only) MCP server that exposes the 9 `research-env-v1`
tools to an MCP client (Goose 1.38) over **stdio JSON-RPC 2.0**. ~250 LOC, no framework.

## How it loads the contract (CANONICAL source — nothing hardcoded except dispatch)

On startup it reads `manifest.json` from the contract dir and, for each entry, loads the
referenced `tools/<name>.json` schema. The tool **set**, descriptions, and `inputSchema`s
all come from those files; the server hardcodes only the _dispatch map_ (name → impl).
Add/rename a tool in the contract → it shows up here on next start, no code change.

- Contract dir: `RESEARCH_ENV_CONTRACT_DIR` (default `~/Pebble/Github/rockie/nugget/contract/research-env-v1`)
- Workspace sandbox: `RESEARCH_ENV_WORKSPACE` (default `./workspace`)

## Stub policy (same as the A3 proof)

| Tool                                                   | Behavior                                                                                     |
| ------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| `read_file`, `list_files`, `write_file`, `run_command` | **REAL** local FS, sandboxed to the workspace dir (paths that escape are rejected).          |
| `web_search`, `fetch_url`, `submit_job`, `get_job`     | **Deterministic STUBS** — no network, no backend. SHA-derived ids so output is reproducible. |
| `finish`                                               | Terminal — echoes the result string.                                                         |

Result-string format follows the contract README: success = raw text or pretty JSON;
error = `{"error":{"code","message"}}` in a single text block, with `isError: true`.

**Wiring the 4 stubs to the real Rockie backend is a LATER slice** — see `../../serving/a3c-mcp.md`.

## Wire protocol

MCP over stdio: newline-delimited JSON-RPC 2.0 (one request/response object per line).
Implements `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`.
Protocol version `2024-11-05`.

## Register in Goose (config.yaml)

Goose reads `~/.config/goose/config.yaml`. The `extensions:` map deserializes into
`ExtensionEntry { enabled: bool, #[flatten] ExtensionConfig }` — **`enabled: true` is
REQUIRED**; omitting it makes the whole entry fail to deserialize and Goose silently
drops it ("Skipping malformed extension config entry"). Working snippet:

```yaml
extensions:
  research-env-v1:
    enabled: true # REQUIRED — without it Goose silently drops the extension
    type: stdio
    name: research-env-v1
    description: Rockie research action space (research-env-v1) — 9 tools over MCP stdio
    cmd: python3
    args:
      - /root/nugget-a3c/research-env-mcp/server.py
    envs:
      RESEARCH_ENV_CONTRACT_DIR: /root/nugget-a3c/research-env-v1
      RESEARCH_ENV_WORKSPACE: /root/nugget-a3c/workspace
    timeout: 300
```

## Local smoke test (no Goose)

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_files","arguments":{}}}' \
  | RESEARCH_ENV_WORKSPACE=/tmp/ws python3 server.py
```
