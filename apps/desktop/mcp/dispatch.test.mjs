import test from 'node:test'
import assert from 'node:assert/strict'
import { dispatchTool, wrapResult } from './dispatch.mjs'

// Minimal mock deps for dispatchTool.
function baseDeps(overrides = {}) {
  const noop = async () => {}
  return {
    EXPECTED_HOME: '/tmp/sb',
    DEFAULT_HOME: '/real',
    connect: async () => ({ send: noop, eval: async () => true, on: noop }),
    assertTargetAttested: async () => {},
    handleAct: async () => ({ ok: true }),
    handleFlow: async () => ({ ok: true }),
    status: async () => ({ cdpAlive: true }),
    inspect: async () => ({ tag: 'div' }),
    query: async () => ([{ i: 0 }]),
    consoleLog: async () => ([]),
    screenshot: async () => ({
      content: [
        { type: 'image', data: 'BASE64DATA', mimeType: 'image/png' },
        { type: 'text', text: JSON.stringify({ bytes: 1234 }) }
      ]
    }),
    readTools: [
      { name: 'desktop_ui_status' },
      { name: 'ui_inspect' },
      { name: 'ui_query' },
      { name: 'ui_console' },
      { name: 'ui_screenshot' }
    ],
    actTools: [{ name: 'ui_click' }, { name: 'ui_type' }, { name: 'ui_press' }, { name: 'ui_eval' }],
    flowTools: [{ name: 'ui_flow_edit' }, { name: 'ui_flow_model_switch' }],
    CFG: { allowAct: true },
    evalBounded: async (e) => 'eval',
    resolveSelector: (s) => s,
    ...overrides
  }
}

test('BUG #1 (RED): ui_screenshot result keeps MCP image content, not stringified JSON', async () => {
  const out = await dispatchTool('ui_screenshot', {}, baseDeps())
  const wrapped = wrapResult(out)
  // The image block must survive into the final MCP response.
  const hasImage = wrapped.content.some((c) => c.type === 'image' && c.data === 'BASE64DATA')
  assert.ok(hasImage, 'expected an image content block in the wrapped result; got ' + JSON.stringify(wrapped.content))
})

test('BUG #1 (RED): wrapResult must not JSON-stringify an object that already has content', async () => {
  const out = { content: [{ type: 'image', data: 'X' }] }
  const wrapped = wrapResult(out)
  assert.ok(wrapped.content[0].type === 'image', 'image block must be preserved verbatim')
  assert.equal(wrapped.content.length, 1)
})

test('dispatch: ui_inspect returns JSON-stringified text block (current behavior preserved)', async () => {
  const out = await dispatchTool('ui_inspect', { selector: 'composer' }, baseDeps())
  const wrapped = wrapResult(out)
  assert.equal(wrapped.content[0].type, 'text')
  assert.equal(JSON.parse(wrapped.content[0].text).tag, 'div')
})

test('dispatch: unknown tool throws', async () => {
  await assert.rejects(() => dispatchTool('nope', {}, baseDeps()), /unknown tool/)
})

// --- status preflight (should-fix, review round 3) ---

test('status preflight: reports cdpAlive:false when CDP is down — connect never called', async () => {
  let connectCalls = 0
  const deps = baseDeps({
    connect: async () => { connectCalls++; throw new Error('No CDP target on :9222') },
    status: async () => ({ cdpAlive: false, mode: 'unavailable', allowAct: false })
  })

  const out = await dispatchTool('desktop_ui_status', {}, deps)
  assert.equal(connectCalls, 0, 'status must not require a connection')
  assert.ok(!out.isError, 'preflight must not be an error')
  const parsed = JSON.parse(out.content[0].text)
  assert.equal(parsed.cdpAlive, false)
  assert.equal(parsed.mode, 'unavailable')
})

test('boundary intact: ui_inspect under the same dead-CDP deps still refuses', async () => {
  const deps = baseDeps({
    connect: async () => { throw new Error('No CDP target on :9222') }
  })

  await assert.rejects(() => dispatchTool('ui_inspect', { selector: 'div' }, deps), /No CDP target/)
})

// --- P1: dispatch → handleAct ctx contract (real handler, not a mock) ---

test('P1 (RED): dispatch → real handleAct reaches CDP input — no ensureCdp hole', async () => {
  const sends = []
  const live = {
    send: async (m, p) => { sends.push({ m, p }); return { ok: true } },
    eval: async () => true,
    on: () => {}
  }
  const realHandleAct = (await import('./tools/act.mjs')).handleAct
  const deps = baseDeps({
    handleAct: realHandleAct,
    CFG: { allowAct: true }
  })
  deps.connect = async () => live // dispatch's connect() result IS the ctx.cdp

  const out = await dispatchTool('ui_click', { selector: '.btn' }, deps)
  assert.ok(sends.length > 0, 'real handleAct must produce CDP input from the dispatch ctx; got: ' + JSON.stringify(out).slice(0, 120))
})
