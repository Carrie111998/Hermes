#!/usr/bin/env node
/**
 * Hermes Desktop Debug MCP server.
 *
 * Gives LLM agents native tools to inspect (and, gated, drive) the live
 * renderer of `apps/desktop` over the dev-only CDP port. Wraps the existing
 * perf-harness client (`scripts/perf/lib/cdp.mjs`) so protocol fixes stay in
 * one place.
 *
 * Read-only by default. Mutating tools require DESKTOP_DEBUG_MCP_ALLOW_ACT=1
 * in the server's environment.
 *
 * Run:  node server.mjs [--port 9222] [--match 5174]
 */
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  CallToolRequestSchema,
  ListToolsRequestSchema
} from '@modelcontextprotocol/sdk/types.js'

import { CDP, SELECTORS, discoverTarget } from '../scripts/perf/lib/cdp.mjs'
import os from 'node:os'
import path from 'node:path'
import { actTools, handleAct } from './tools/act.mjs'
import { flowTools, handleFlow } from './tools/flows.mjs'

// ---------------------------------------------------------------------------
// Output bounds — never dump the whole DOM into an agent's context.
const MAX_TEXT = 80 // per-node text snippet length
const MAX_NODES = 20 // ui_query row cap
const MAX_EVAL = 4000 // ui_eval output cap (chars)
const MAX_CONSOLE = 50

const args = process.argv.slice(2)
const argOf = (name, fallback) => {
  const i = args.indexOf(name)
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback
}

const CFG = {
  port: Number(argOf('--port', process.env.DESKTOP_DEBUG_MCP_PORT || '9222')),
  match: argOf('--match', '5174'),
  allowAct: process.env.DESKTOP_DEBUG_MCP_ALLOW_ACT === '1'
}

let cdp = null // lazily connected CDP instance
const consoleRing = [] // renderer console capture (bounded)

// The HERMES_HOME the operator declares this desktop instance is running
// against. Mutating tools refuse to run unless this is set AND differs from
// the operator's real default home — see assertSandboxed(). This is the rail
// that prevents a debug MCP run from reading/writing the operator's real API
// keys and chat history (the 2026-08-26 incident: a manual electron launch
// with only HERMES_DESKTOP_USER_DATA_DIR set silently used ~/.hermes).
const EXPECTED_HOME = process.env.DESKTOP_DEBUG_MCP_EXPECTED_HOME || ''
const DEFAULT_HOME = process.env.HERMES_HOME || path.join(os.homedir(), '.hermes')

/**
 * Fail-closed safety rail for mutating tools.
 *
 * A debug MCP run must target an isolated sandbox (its own HERMES_HOME), never
 * the operator's real data. We cannot reliably read the target's HERMES_HOME
 * from the renderer (it is not exposed), so we require the operator to DECLARE
 * it: set DESKTOP_DEBUG_MCP_EXPECTED_HOME to the sandbox path when launching
 * the server. If unset, or if it equals the default home, mutating tools are
 * refused with a clear instruction.
 */
function assertSandboxed() {
  if (!EXPECTED_HOME) {
    throw new Error(
      'REFUSED: DESKTOP_DEBUG_MCP_EXPECTED_HOME is not set. The debug MCP ' +
        'server will not mutate a desktop instance unless you declare which ' +
        'isolated HERMES_HOME it is running against. Launch with ' +
        'DESKTOP_DEBUG_MCP_EXPECTED_HOME=/tmp/your-sandbox-home and ensure the ' +
        'desktop instance was started with the same HERMES_HOME. Never point ' +
        'this at your real ~/.hermes.'
    )
  }
  if (EXPECTED_HOME === DEFAULT_HOME) {
    throw new Error(
      `REFUSED: declared HERMES_HOME (${EXPECTED_HOME}) is the default home. ` +
        'The debug MCP server must target an isolated sandbox, not your real data.'
    )
  }
}

async function connect() {
  if (cdp) return cdp

  try {
    await discoverTarget({ port: CFG.port, match: CFG.match, timeoutMs: 3000 })
  } catch {
    throw new Error(
      `No CDP target on :${CFG.port}. The debug port only exists for DEV runs ` +
        '(packaged builds never open it). Either ask the user to start the dev ' +
        "server (`cd apps/desktop && npm run dev`) or launch an isolated probe " +
        'instance (see apps/desktop/mcp/README.md).'
    )
  }

  cdp = await CDP.connect({ port: CFG.port, match: CFG.match })
  cdp.on('Runtime.consoleAPICalled', p => {
    consoleRing.push({
      level: p.type,
      text: p.args?.map(a => a.value ?? a.description ?? '').join(' ').slice(0, 200),
      t: Date.now()
    })
    if (consoleRing.length > 200) consoleRing.shift()
  })

  return cdp
}

/** Evaluate with a bounded JSON result. Throws with a friendly message on failure. */
async function evalBounded(expression) {
  const c = await connect()
  const out = await c.eval(expression)
  const s = typeof out === 'string' ? out : JSON.stringify(out)
  return s.length > MAX_EVAL ? s.slice(0, MAX_EVAL) + '…[truncated]' : s
}

/** Resolve a selector: either a SELECTORS key or a raw CSS selector. */
const resolveSelector = sel => SELECTORS[sel] || sel

// ---------------------------------------------------------------------------
// Read-only tool implementations

async function status() {
  let alive = false
  let targets = []

  try {
    const list = await (await fetch(`http://127.0.0.1:${CFG.port}/json/list`)).json()
    targets = list.filter(t => t.type === 'page').map(t => ({ url: String(t.url).slice(0, 120), title: String(t.title).slice(0, 60) }))
    alive = targets.length > 0
  } catch {
    alive = false
  }

  return {
    cdpAlive: alive,
    port: CFG.port,
    mode: alive ? 'dev' : 'unavailable',
    allowAct: CFG.allowAct,
    selectors: Object.keys(SELECTORS),
    targets
  }
}

async function inspect({ selector }) {
  const sel = resolveSelector(selector)
  return evalBounded(`(() => {
    const el = document.querySelector(${JSON.stringify(sel)})
    if (!el) return null
    const cs = getComputedStyle(el)
    const box = el.getBoundingClientRect()
    const ownClasses = typeof el.className === 'string' ? el.className : ''
    // Walk up a few ancestors: inherited styles are the classic "why won't it apply" trap.
    const parents = []
    let n = el.parentElement
    while (n && parents.length < 5) { parents.push(n.className); n = n.parentElement }
    return JSON.stringify({
      tag: el.tagName.toLowerCase(),
      id: el.id || undefined,
      classes: ownClasses,
      box: { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width), h: Math.round(box.height) },
      visible: !!(box.width || box.height) && cs.display !== 'none' && cs.visibility !== 'hidden',
      computed: { display: cs.display, position: cs.position, fontSize: cs.fontSize, fontWeight: cs.fontWeight, color: cs.color, background: cs.backgroundColor },
      inheritedHint: ownClasses ? 'own classes present' : 'NO own class — value is inherited; fix the ancestor rule',
      ancestors: parents
    })
  })()`)
}

async function query({ selector, limit }) {
  const sel = resolveSelector(selector)
  const cap = Math.min(limit || MAX_NODES, MAX_NODES)
  return evalBounded(`(() => {
    const els = [...document.querySelectorAll(${JSON.stringify(sel)})].slice(0, ${cap})
    return els.map((el, i) => {
      const b = el.getBoundingClientRect()
      const txt = (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, ${MAX_TEXT})
      return { i, text: txt, w: Math.round(b.width), h: Math.round(b.height), visible: b.width > 0 }
    })
  })()`)
}

async function consoleLog({ level, sinceMs }) {
  const cutoff = sinceMs ? Date.now() - sinceMs : 0
  let rows = consoleRing.filter(r => r.t >= cutoff)
  if (level) rows = rows.filter(r => r.level === level)
  return rows.slice(-MAX_CONSOLE)
}

async function screenshot({ path }) {
  const c = await connect()
  const shot = await c.send('Page.captureScreenshot', { format: 'png' })
  const file = path || `/tmp/desktop-debug-mcp/screen-${Date.now()}.png`
  const fs = await import('node:fs')
  fs.mkdirSync(file.substring(0, file.lastIndexOf('/')), { recursive: true })
  fs.writeFileSync(file, Buffer.from(shot.data, 'base64'))
  return { savedTo: file, bytes: shot.data.length }
}

// ---------------------------------------------------------------------------

const readTools = [
  {
    name: 'desktop_ui_status',
    description:
      'Check whether the Hermes desktop dev app has its CDP debug port alive, and which page targets/selectors are available. Call this FIRST before any other desktop UI tool.',
    inputSchema: { type: 'object', properties: {} }
  },
  {
    name: 'ui_inspect',
    description:
      'Inspect ONE element in the Hermes desktop renderer: tag, classes, bounding box, visibility, key computed styles, plus an inheritance hint (own classes vs inherited rule). Selector may be a stable key (composer, threadViewport, assistantMessage, turnPair, profileRail) or any CSS selector.',
    inputSchema: {
      type: 'object',
      properties: { selector: { type: 'string', description: 'SELECTORS key or CSS selector' } },
      required: ['selector']
    }
  },
  {
    name: 'ui_query',
    description:
      'List up to 20 elements matching a selector with bounded text snippets and visibility. Good for "what messages exist in the thread right now".',
    inputSchema: {
      type: 'object',
      properties: {
        selector: { type: 'string' },
        limit: { type: 'number', description: 'max nodes (hard cap 20)' }
      },
      required: ['selector']
    }
  },
  {
    name: 'ui_console',
    description: 'Recent renderer console output captured while connected.',
    inputSchema: {
      type: 'object',
      properties: {
        level: { type: 'string', description: 'error|warning|log|info' },
        sinceMs: { type: 'number' }
      }
    }
  },
  {
    name: 'ui_screenshot',
    description: 'Capture the current window as PNG to a path (default under /tmp/desktop-debug-mcp). Returns the path.',
    inputSchema: { type: 'object', properties: { path: { type: 'string' } } }
  }
]

const allTools = [...readTools, ...actTools, ...flowTools]

const server = new Server(
  { name: 'hermes-desktop-debug', version: '0.1.0' },
  { capabilities: { tools: {} } }
)

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: allTools.map(t => ({
    name: t.name,
    description:
      t.gated && !CFG.allowAct
        ? `${t.description} [DISABLED: set DESKTOP_DEBUG_MCP_ALLOW_ACT=1 to enable]`
        : t.description,
    inputSchema: t.inputSchema
  }))
}))

/** Shared lazy CDP handle for tool modules (act/flow). Uses the friendly connect(). */
async function getCdp() {
  return connect()
}

const toolCtx = {
  evalBounded,
  resolveSelector,
  get cdp() {
    return cdp
  },
  ensureCdp: getCdp
}

server.setRequestHandler(CallToolRequestSchema, async req => {
  const { name, arguments: a = {} } = req.params

  try {
    let out
    if (readTools.some(t => t.name === name)) {
      out = name === 'status' ? undefined : undefined
      // dispatch read tools
      if (name === 'desktop_ui_status') out = await status()
      else if (name === 'ui_inspect') out = await inspect(a)
      else if (name === 'ui_query') out = await query(a)
      else if (name === 'ui_console') out = await consoleLog(a)
      else if (name === 'ui_screenshot') out = await screenshot(a)
    } else if (actTools.some(t => t.name === name)) {
      if (!CFG.allowAct) {
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                error: 'mutating tools are disabled',
                hint: "set DESKTOP_DEBUG_MCP_ALLOW_ACT=1 in this MCP server's env to enable ui_click/ui_type/ui_press/ui_eval"
              })
            }
          ]
        }
      }
      const live = await connect()
      assertSandboxed()
      out = await handleAct(name, a, { ...toolCtx, cdp: live })
    } else if (flowTools.some(t => t.name === name)) {
      if (!CFG.allowAct) {
        return {
          content: [{ type: 'text', text: JSON.stringify({ error: 'flows mutate the UI — disabled without DESKTOP_DEBUG_MCP_ALLOW_ACT=1' }) }]
        }
      }
      const live = await connect()
      assertSandboxed()
      out = await handleFlow(name, a, { ...toolCtx, cdp: live })
    } else {
      throw new Error(`unknown tool: ${name}`)
    }

    const text = typeof out === 'string' ? out : JSON.stringify(out, null, 1)
    return { content: [{ type: 'text', text }] }
  } catch (err) {
    return {
      content: [{ type: 'text', text: JSON.stringify({ error: String(err.message || err) }) }],
      isError: true
    }
  }
})

const transport = new StdioServerTransport()
await server.connect(transport)
console.error(`[desktop-debug-mcp] ready on :${CFG.port} allowAct=${CFG.allowAct}`)
