#!/usr/bin/env python3
"""research-env-v1 stdio MCP server — thin shim for the Rockie research action space.

A dependency-free MCP server (JSON-RPC 2.0 over stdio, framed by Content-Length
headers per the MCP/LSP wire format). It:

  1. Loads the 9 tool schemas from the contract manifest (CANONICAL source) at
     ~/Pebble/Github/rockie/nugget/contract/research-env-v1/ (overridable via
     RESEARCH_ENV_CONTRACT_DIR), hardcoding NOTHING about the tool set except dispatch.
  2. Advertises them via MCP `tools/list`.
  3. Dispatches `tools/call`.

Side-effect policy (matches the A3 proof):
  - REAL local-FS, sandboxed to a workspace dir (RESEARCH_ENV_WORKSPACE, default ./workspace):
        read_file, list_files, write_file, run_command
  - Deterministic STUBS (no network / no backend):
        web_search, fetch_url, submit_job, get_job
  - finish: terminal tool, echoes the result.

Result-string format (contract README § Result-string format):
  - success: raw text, or pretty-printed JSON (2-space) for structured tools
  - error:   {"error":{"code":<stable_string>,"message":<prose>}}  as a text block

Wiring the stubs to the real Rockie backend (web_search/fetch_url/submit_job/get_job)
is a LATER slice — see a3c-mcp.md.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "research-env-v1"
SERVER_VERSION = "0.1.0"

CONTRACT_DIR = Path(
    os.environ.get(
        "RESEARCH_ENV_CONTRACT_DIR",
        Path.home() / "Pebble/Github/rockie/nugget/contract/research-env-v1",
    )
).expanduser()
WORKSPACE = Path(os.environ.get("RESEARCH_ENV_WORKSPACE", "./workspace")).expanduser().resolve()


def log(msg):
    print(f"[research-env-mcp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Contract loading — the tool SET comes entirely from the manifest + schemas.
# ---------------------------------------------------------------------------
def load_tools():
    manifest = json.loads((CONTRACT_DIR / "manifest.json").read_text())
    tools = []
    for entry in manifest["tools"]:
        schema = json.loads((CONTRACT_DIR / entry["schema"]).read_text())
        tools.append(
            {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "inputSchema": schema["inputSchema"],
            }
        )
    return tools


TOOLS = load_tools()


# ---------------------------------------------------------------------------
# Workspace sandbox helpers
# ---------------------------------------------------------------------------
def _safe_path(rel: str) -> Path:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    p = (WORKSPACE / rel).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ValueError(f"path escapes workspace sandbox: {rel}")
    return p


class ToolError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Tool implementations (real FS for 4, deterministic stubs for 4, terminal finish)
# ---------------------------------------------------------------------------
def t_read_file(args):
    p = _safe_path(args["path"])
    if not p.is_file():
        raise ToolError("tool_error", f"not a file: {args['path']}")
    return p.read_text()


def t_list_files(args):
    base = _safe_path(args.get("path", "."))
    if not base.exists():
        raise ToolError("tool_error", f"no such path: {args.get('path', '.')}")
    if base.is_file():
        return str(base.relative_to(WORKSPACE))
    out = []
    for f in sorted(base.rglob("*")):
        if f.is_file():
            out.append(str(f.relative_to(WORKSPACE)))
    return "\n".join(out) if out else "(empty)"


def t_write_file(args):
    p = _safe_path(args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    content = args.get("content", "")
    p.write_text(content)
    return f"wrote {args['path']} ({len(content.encode('utf-8'))} bytes)"


def t_run_command(args):
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    timeout = int(args.get("timeout_sec", 120))
    try:
        r = subprocess.run(
            args["command"], shell=True, cwd=str(WORKSPACE),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError("tool_error", f"command timed out after {timeout}s")
    return f"{r.stdout}{r.stderr}\n[exit {r.returncode}]"


def _det(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:8]


def t_web_search(args):
    q = args["query"]
    k = int(args.get("k", 10))
    h = _det(q)
    results = [
        {
            "title": f"[STUB] Result {i+1} for {q!r}",
            "url": f"https://stub.research-env.local/{h}/{i+1}",
            "snippet": f"Deterministic stub snippet {i+1} for query {q!r}. "
                       "Wire to the real Rockie backend in a later slice.",
        }
        for i in range(min(k, 3))
    ]
    return json.dumps(results, indent=2)


def t_fetch_url(args):
    url = args["url"]
    return (
        f"[STUB] Extracted main-content text for {url}\n"
        f"(deterministic id {_det(url)}). Wire to the real fetch backend later."
    )


def t_submit_job(args):
    seed = f"{args['hardware']}|{args['image']}|{args['command']}"
    return json.dumps({"handle": f"job-{_det(seed)}"}, indent=2)


def t_get_job(args):
    return json.dumps(
        {
            "handle": args["handle"],
            "state": "succeeded",
            "progress": 1.0,
            "metrics": {"stub": True},
            "logs": "[STUB] job logs — wire to real backend later.",
            "artifacts": [],
        },
        indent=2,
    )


def t_finish(args):
    return f"[finished] {args['result']}"


DISPATCH = {
    "read_file": t_read_file,
    "list_files": t_list_files,
    "write_file": t_write_file,
    "run_command": t_run_command,
    "web_search": t_web_search,
    "fetch_url": t_fetch_url,
    "submit_job": t_submit_job,
    "get_job": t_get_job,
    "finish": t_finish,
}


def call_tool(name, args):
    """Returns (text, is_error)."""
    fn = DISPATCH.get(name)
    if fn is None:
        return json.dumps({"error": {"code": "unknown_tool", "message": f"no such tool: {name}"}}), True
    try:
        return fn(args or {}), False
    except ToolError as e:
        return json.dumps({"error": {"code": e.code, "message": e.message}}), True
    except Exception as e:  # noqa: BLE001 — surface any impl failure in the contract envelope
        return json.dumps({"error": {"code": "tool_error", "message": str(e)}}), True


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------
def handle(req):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method in ("notifications/initialized", "initialized"):
        return None  # notification, no response
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = req.get("params", {})
        text, is_error = call_tool(params.get("name"), params.get("arguments"))
        return {
            "jsonrpc": "2.0", "id": rid,
            "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
        }
    if rid is None:
        return None  # unknown notification — ignore
    return {
        "jsonrpc": "2.0", "id": rid,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


# ---------------------------------------------------------------------------
# stdio transport: newline-delimited JSON (MCP stdio framing per spec)
# ---------------------------------------------------------------------------
def main():
    log(f"start: {len(TOOLS)} tools from {CONTRACT_DIR}, workspace={WORKSPACE}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            resp = handle(req)
        except Exception as e:  # noqa: BLE001
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
