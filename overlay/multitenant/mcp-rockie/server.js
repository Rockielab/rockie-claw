#!/usr/bin/env node
/**
 * mcp-rockie — MCP server giving claude/codex access to the tenant's
 * Rockie tool surface (labs, sources, notes, artifacts, search, compute,
 * emit_artifact fan-out).
 *
 * The tool catalog mirrors `platform-context/api/agent_tools/schemas.py`
 * (single source of truth — keep in lockstep; a parity test will fail
 * on drift). Every tool call PROXIES to platform-context via the per-
 * tenant HTTP surface at /api/agent-tools/{name}; the broker already
 * has the same env vars wired:
 *
 *   ROCKIELAB_API_BASE          (default https://api.rockielab.com)
 *   ROCKIELAB_API_PASSWORD      (mirrors OPEN_NOTEBOOK_PASSWORD)
 *   ROCKIELAB_TENANT_DEV_TOKEN  (per-tenant; eventually a signed JWT)
 *
 * Registered into ~/.claude/mcp.json + ~/.codex/mcp.json at image
 * build time (see Dockerfile.multitenant + assemble-skills.sh).
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'

const API_BASE =
  process.env.ROCKIELAB_API_BASE || 'https://api.rockielab.com'
const API_PASSWORD =
  process.env.ROCKIELAB_API_PASSWORD || process.env.OPEN_NOTEBOOK_PASSWORD || ''
const TENANT_TOKEN = process.env.ROCKIELAB_TENANT_DEV_TOKEN || ''

// Tool catalog. Keep in lockstep with
// platform-context/api/agent_tools/schemas.py. A parity test in
// platform-context/tests/test_agent_tools.py asserts the name sets
// match.
const TOOLS = [
  {
    name: 'notebook_read',
    description:
      "Read a lab's metadata + a summary of its sources and notes. Use when the user names a specific lab or you need to ground the next action in a lab's contents.",
    inputSchema: {
      type: 'object',
      properties: { notebook_id: { type: 'string' } },
      required: ['notebook_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'notebook_create',
    description:
      'Create a new lab (notebook). Use sparingly; only when the user explicitly asks for a new workspace.',
    inputSchema: {
      type: 'object',
      properties: {
        name: { type: 'string', minLength: 1, maxLength: 200 },
        description: { type: 'string', default: '' },
      },
      required: ['name'],
      additionalProperties: false,
    },
  },
  {
    name: 'notebook_update',
    description:
      'Rename or repitch a lab. Provide at least one of name/description.',
    inputSchema: {
      type: 'object',
      properties: {
        notebook_id: { type: 'string' },
        name: { type: 'string' },
        description: { type: 'string' },
      },
      required: ['notebook_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'source_ingest',
    description:
      "Ingest content into a lab. Pass exactly one of url / text / file_path. Delegates to the platform's content extraction pipeline.",
    inputSchema: {
      type: 'object',
      properties: {
        notebook_id: { type: 'string' },
        url: { type: 'string' },
        text: { type: 'string' },
        file_path: { type: 'string' },
        title: { type: 'string' },
      },
      required: ['notebook_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'source_read',
    description:
      "Read one source's extracted text + metadata. Heavyweight; only call when the listing preview is not enough.",
    inputSchema: {
      type: 'object',
      properties: { source_id: { type: 'string' } },
      required: ['source_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'note_create',
    description:
      'Create a note in a lab. Use to persist a hypothesis, finding, or running summary so the user can see it later.',
    inputSchema: {
      type: 'object',
      properties: {
        notebook_id: { type: 'string' },
        body: { type: 'string', minLength: 1 },
        title: { type: 'string' },
        source_ids: { type: 'array', items: { type: 'string' } },
      },
      required: ['notebook_id', 'body'],
      additionalProperties: false,
    },
  },
  {
    name: 'note_update',
    description: "Edit an existing note's body or title.",
    inputSchema: {
      type: 'object',
      properties: {
        note_id: { type: 'string' },
        body: { type: 'string' },
        title: { type: 'string' },
      },
      required: ['note_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'note_delete',
    description:
      'Delete a note. Idempotent — deleting a missing note is not an error.',
    inputSchema: {
      type: 'object',
      properties: { note_id: { type: 'string' } },
      required: ['note_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'insight_list',
    description: 'List insights derived from a source.',
    inputSchema: {
      type: 'object',
      properties: { source_id: { type: 'string' } },
      required: ['source_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'transformation_execute',
    description:
      'Apply a named transformation graph to a source. Returns a command id; poll job_status to follow it.',
    inputSchema: {
      type: 'object',
      properties: {
        source_id: { type: 'string' },
        transformation_id: { type: 'string' },
      },
      required: ['source_id', 'transformation_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'podcast_generate',
    description:
      'Generate a podcast episode (outline + transcript + TTS). Returns a job id; poll job_status for completion.',
    inputSchema: {
      type: 'object',
      properties: {
        notebook_id: { type: 'string' },
        profile_id: { type: 'string' },
        episode_name: { type: 'string' },
      },
      required: ['notebook_id', 'profile_id', 'episode_name'],
      additionalProperties: false,
    },
  },
  {
    name: 'artifact_list',
    description: 'List artifacts attached to a lab.',
    inputSchema: {
      type: 'object',
      properties: {
        notebook_id: { type: 'string' },
        kind: { type: 'string' },
        limit: { type: 'integer', minimum: 1, maximum: 100, default: 25 },
      },
      required: ['notebook_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'artifact_retrieve',
    description: 'Fetch artifact metadata + a URL or inline body.',
    inputSchema: {
      type: 'object',
      properties: { artifact_id: { type: 'string' } },
      required: ['artifact_id'],
      additionalProperties: false,
    },
  },
  {
    name: 'search_query',
    description:
      "Vector + text search over the tenant's sources + notes. Returns top-k hits with relevance scores. Use for grounding answers in the user's corpus.",
    inputSchema: {
      type: 'object',
      properties: {
        query: { type: 'string', minLength: 1 },
        notebook_id: { type: 'string' },
        k: { type: 'integer', minimum: 1, maximum: 50, default: 10 },
      },
      required: ['query'],
      additionalProperties: false,
    },
  },
  {
    name: 'ask_question',
    description:
      'Multi-stage Ask workflow over the corpus (search → reason → synthesize). Heavier than search_query.',
    inputSchema: {
      type: 'object',
      properties: {
        query: { type: 'string', minLength: 1 },
        notebook_id: { type: 'string' },
      },
      required: ['query'],
      additionalProperties: false,
    },
  },
  {
    name: 'experiment_submit',
    description:
      "Dispatch an experiment to the tenant's configured compute target (rockie_gpu / byo_ssh / byo_github / artifact_only).",
    inputSchema: {
      type: 'object',
      properties: {
        script: { type: 'string', minLength: 1 },
        env: { type: 'object' },
        timeout_sec: { type: 'integer', minimum: 1, maximum: 86400 },
        gpu_type: { type: 'string' },
      },
      required: ['script'],
      additionalProperties: false,
    },
  },
  {
    name: 'job_status',
    description:
      'Poll a previously-submitted experiment by its handle. Returns state + progress + any artifacts produced.',
    inputSchema: {
      type: 'object',
      properties: { handle: { type: 'string' } },
      required: ['handle'],
      additionalProperties: false,
    },
  },
  {
    name: 'emit_artifact',
    description:
      "Publish an artifact to up to four destinations in parallel (chat, ui, github, huggingface). Each destination's success/failure is independent so the agent can retry one channel without re-doing the successful ones.",
    inputSchema: {
      type: 'object',
      properties: {
        kind: {
          type: 'string',
          enum: [
            'plot',
            'table',
            'markdown',
            'model_weights',
            'paper_pdf',
            'dataset',
            'code',
            'podcast_episode',
          ],
        },
        content: { type: 'string' },
        title: { type: 'string', minLength: 1, maxLength: 200 },
        notebook_id: { type: 'string' },
        destinations: {
          type: 'array',
          items: {
            type: 'string',
            enum: ['chat', 'ui', 'github', 'huggingface'],
          },
          default: ['chat', 'ui'],
          minItems: 1,
        },
        github_target: {
          type: 'object',
          properties: {
            repo: { type: 'string' },
            path: { type: 'string' },
            branch: { type: 'string', default: 'main' },
            message: { type: 'string' },
          },
          required: ['repo', 'path'],
          additionalProperties: false,
        },
        huggingface_target: {
          type: 'object',
          properties: {
            repo: { type: 'string' },
            path: { type: 'string' },
            kind: {
              type: 'string',
              enum: ['model', 'dataset', 'space'],
              default: 'dataset',
            },
          },
          required: ['repo'],
          additionalProperties: false,
        },
      },
      required: ['kind', 'content', 'title', 'notebook_id'],
      additionalProperties: false,
    },
  },
]

function authHeaders() {
  const h = { 'Content-Type': 'application/json' }
  if (API_PASSWORD) h['Authorization'] = `Bearer ${API_PASSWORD}`
  if (TENANT_TOKEN) h['X-Tenant-Token'] = TENANT_TOKEN
  return h
}

async function callTool(name, args) {
  const url = `${API_BASE}/api/agent-tools/${encodeURIComponent(name)}`
  const r = await fetch(url, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({ arguments: args || {} }),
  })
  const text = await r.text()
  if (!r.ok) {
    let detail
    try {
      detail = JSON.parse(text)
    } catch {
      detail = { error: { code: 'http_error', message: text.slice(0, 240) } }
    }
    const err = new Error(
      detail.detail?.error?.message ||
        detail.error?.message ||
        `${name} → ${r.status}`,
    )
    err.status = r.status
    err.code = detail.detail?.error?.code || detail.error?.code || 'http_error'
    throw err
  }
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

const server = new Server(
  { name: 'mcp-rockie', version: '0.2.0' },
  { capabilities: { tools: {} } },
)

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }))

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const { name, arguments: args = {} } = req.params
  // Quick local check so unknown-tool errors don't make a round-trip.
  if (!TOOLS.find((t) => t.name === name)) {
    return {
      content: [
        {
          type: 'text',
          text: JSON.stringify({ error: { code: 'unknown_tool', message: `unknown tool: ${name}` } }),
        },
      ],
      isError: true,
    }
  }
  try {
    const result = await callTool(name, args)
    return {
      content: [
        {
          type: 'text',
          text:
            typeof result === 'string'
              ? result
              : JSON.stringify(result, null, 2).slice(0, 32000),
        },
      ],
    }
  } catch (err) {
    return {
      content: [
        {
          type: 'text',
          text: JSON.stringify({
            error: {
              code: err?.code || 'tool_error',
              message: err?.message || String(err),
            },
          }),
        },
      ],
      isError: true,
    }
  }
})

const transport = new StdioServerTransport()
await server.connect(transport)
