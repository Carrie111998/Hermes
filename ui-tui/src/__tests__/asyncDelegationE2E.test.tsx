import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { applyAsyncList, getAsyncDelegations, resetAsyncDelegations } from '../app/delegationStore.js'
import { dispatchSteer, type SteerDispatchDeps } from '../app/submissionCore.js'
import { getTurnState, patchTurnState, resetTurnState } from '../app/turnStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { LiveAgentsPanel } from '../components/agentsPanel.js'
import { HOTKEYS } from '../content/hotkeys.js'
import type { GatewayClient } from '../gatewayClient.js'
import { completionRequestForInput, steerCompletionsForInput } from '../hooks/useCompletion.js'
import { buildAgentRows, DONE_LINGER_MS } from '../lib/agentRows.js'
import { parseSteerCommand, resolveAsyncSteerTargetId, resolveSteerTargetId } from '../lib/subagentSteer.js'
import { stripAnsi } from '../lib/text.js'
import type { SubagentProgress } from '../types.js'

// ─────────────────────────────────────────────────────────────────────
// End-to-end battery for the docked agents panel + live steering.
//
// Every other suite tests one seam in isolation (rows, panel, store,
// steer parser, submission). This one wires the seams together in the
// order production does and asserts on the two things a user actually
// experiences: the painted frame, and the RPC that leaves the client.
//
//   delegation.async_list payload
//     → applyAsyncList (store)
//       → buildAgentRows (projection)
//         → LiveAgentsPanel (paint)
//
//   typed text
//     → steerCompletionsForInput (what the panel offers)
//       → parseSteerCommand + resolve*TargetId
//         → dispatchSteer (subagent.send / delegation.send)
// ─────────────────────────────────────────────────────────────────────

/** Render through real Ink and return the painted frame, exactly like the
 * agentsPanel suite — layout (wrap, truncate, height) is Yoga's doing and only
 * shows up in the frame. */
const renderFrame = (element: React.ReactElement, columns = 72): string[] => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  const frames: string[] = []

  Object.assign(stdout, { columns, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    frames.push(chunk.toString())
  })

  const instance = renderSync(element, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  instance.unmount()
  instance.cleanup()

  const painted = frames.filter(frame => stripAnsi(frame).trim() !== '').at(-1) ?? ''

  return stripAnsi(painted)
    .split('\n')
    .map(line => line.replace(/\s+$/, ''))
    .filter(line => line !== '')
}

// The registry stamps `dispatched_at` / `completed_at` with `time.time()` —
// epoch SECONDS — while the panel's clock is `Date.now()` in ms. Keeping both
// units explicit here is the point: a fixture that quietly mixed them would
// make elapsed and the done-linger look broken (or, worse, look fine).
const T0_S = 1_000_000
const T0_MS = T0_S * 1000

/** The literal JSON body `delegation.async_list` returns (tui_gateway/server.py
 * `_ok(rid, {"delegations": ..., "running": ...})`). Written out longhand
 * rather than built from a helper so a server-side rename of a field breaks
 * this test the way it would break the panel. */
const asyncListPayload = (over: Record<string, unknown> = {}) => ({
  delegations: [
    {
      completed_at: null,
      delegation_id: 'b7c2a3f1',
      depth: 1,
      dispatched_at: T0_S,
      goal: 'patch token-bucket refill race',
      model: 'opus-4.1',
      role: 'fixer',
      status: 'running'
    }
  ],
  running: 1,
  ...over
})

const liveSub = (over: Partial<SubagentProgress> = {}): SubagentProgress => ({
  depth: 1,
  goal: 'map auth handshake edge cases',
  id: 'a11c9d2e',
  index: 0,
  notes: [],
  startedAt: T0_MS,
  status: 'running',
  taskCount: 0,
  thinking: [],
  toolCount: 3,
  tools: ['read_file'],
  ...over
})

/** Gateway double that records every call and lets each method's outcome be
 * chosen per test. */
function makeGateway(handler: (method: string, params: unknown) => unknown = () => ({ delivered: true })) {
  const calls: { method: string; params: any }[] = []

  const gw = {
    request: vi.fn((method: string, params: unknown) => {
      calls.push({ method, params })

      try {
        return Promise.resolve(handler(method, params))
      } catch (err) {
        return Promise.reject(err)
      }
    })
  } as unknown as GatewayClient

  return { calls, gw }
}

function makeSteerDeps(gw: GatewayClient, over: Partial<SteerDispatchDeps> = {}): SteerDispatchDeps {
  return {
    appendMessage: vi.fn(),
    clearIn: vi.fn(),
    gw,
    pushHistory: vi.fn(),
    sys: vi.fn(),
    ...over
  }
}

/** The production submit path, minus React: exactly what useSubmission does for
 * a `@<id> text` input. Kept in one place so every steer test exercises the
 * same resolution order the composer uses. */
const submitAsUser = (full: string, deps: SteerDispatchDeps): boolean => {
  const cmd = parseSteerCommand(full)

  if (!cmd) {
    return false
  }

  return dispatchSteer(
    cmd,
    {
      delegationId: resolveAsyncSteerTargetId(cmd.token, getAsyncDelegations()),
      subagentId: resolveSteerTargetId(cmd.token, getTurnState().subagents)
    },
    full,
    deps
  )
}

const setSubagents = (subs: SubagentProgress[]) => patchTurnState({ subagents: subs })

beforeEach(() => {
  resetAsyncDelegations()
  resetTurnState()
  resetUiState()
})

// ── A. Data flow: RPC payload → store → rows ─────────────────────────

describe('e2e data flow — delegation.async_list payload reaches the panel projection', () => {
  it('carries a server-shaped record through the store into a painted row', () => {
    applyAsyncList(asyncListPayload() as any)

    const { rows, running } = buildAgentRows([], getAsyncDelegations(), T0_MS + 42_000)

    expect(running).toBe(1)
    expect(rows).toHaveLength(1)
    expect(rows[0].key).toBe('async:b7c2a3f1')
    expect(rows[0].goal).toContain('token-bucket')
    // dispatched_at (epoch seconds) is the only clock in the record — elapsed
    // is derived at render time against the panel's ms clock.
    expect(rows[0].elapsedSeconds).toBe(42)
  })

  it('an unchanged poll leaves the snapshot identical so the app does not repaint', () => {
    applyAsyncList(asyncListPayload() as any)

    const first = getAsyncDelegations()

    applyAsyncList(asyncListPayload() as any)

    expect(getAsyncDelegations()).toBe(first)
  })

  it('a status transition in the payload does replace the snapshot', () => {
    applyAsyncList(asyncListPayload() as any)

    const first = getAsyncDelegations()

    applyAsyncList(
      asyncListPayload({
        delegations: [{ ...asyncListPayload().delegations[0], completed_at: T0_S + 60, status: 'completed' }],
        running: 0
      }) as any
    )

    expect(getAsyncDelegations()).not.toBe(first)
    expect(buildAgentRows([], getAsyncDelegations(), T0_MS + 100_000).rows[0].resultReady).toBe(true)
  })

  it('survives a record that omits every optional field', () => {
    applyAsyncList({ delegations: [{ delegation_id: 'z9', status: 'running' }], running: 1 } as any)

    const { rows } = buildAgentRows([], getAsyncDelegations(), 5_000_000)

    expect(rows).toHaveLength(1)
    expect(() => renderFrame(<LiveAgentsPanel cols={72} />)).not.toThrow()
  })

  it('a malformed body (no delegations array) empties the panel instead of crashing', () => {
    applyAsyncList(asyncListPayload() as any)
    applyAsyncList({ running: 3 } as any)

    expect(getAsyncDelegations()).toEqual([])
    expect(renderFrame(<LiveAgentsPanel cols={72} />)).toEqual([])
  })
})

// ── B. E2E paint: stores → LiveAgentsPanel frame ─────────────────────

describe('e2e paint — the connected panel renders what the stores hold', () => {
  it('paints nothing at all when no agent exists (no reserved terminal row)', () => {
    expect(renderFrame(<LiveAgentsPanel cols={72} />)).toEqual([])
  })

  it('paints a background delegation dispatched by the daemon', () => {
    applyAsyncList(asyncListPayload() as any)

    const frame = renderFrame(<LiveAgentsPanel cols={80} />)
    const text = frame.join('\n')

    expect(text).toContain('agents')
    expect(text).toContain('1 running')
    expect(text).toContain('token-bucket')
  })

  it('paints live in-turn subagents above background delegations', () => {
    setSubagents([liveSub()])
    applyAsyncList(asyncListPayload() as any)

    const frame = renderFrame(<LiveAgentsPanel cols={80} />)
    const body = frame.slice(1)

    expect(body[0]).toContain('handshake')
    expect(body[1]).toContain('token-bucket')
    expect(frame[0]).toContain('2 running')
  })

  it('never paints a row wider than the terminal, across a sweep of widths', () => {
    setSubagents([liveSub({ goal: 'x'.repeat(200) })])
    applyAsyncList(asyncListPayload({ delegations: [{ ...asyncListPayload().delegations[0], goal: 'y'.repeat(200) }] }) as any)

    for (const cols of [24, 30, 36, 48, 60, 72, 100, 140]) {
      for (const line of renderFrame(<LiveAgentsPanel cols={cols} />)) {
        expect(line.length, `width ${cols}: "${line}"`).toBeLessThanOrEqual(cols)
      }
    }
  })

  it('a finished delegation still paints while it lingers, and is gone after', () => {
    const doneAtS = T0_S + 60
    const doneAtMs = doneAtS * 1000

    applyAsyncList(
      asyncListPayload({
        delegations: [{ ...asyncListPayload().delegations[0], completed_at: doneAtS, status: 'completed' }],
        running: 0
      }) as any
    )

    expect(buildAgentRows([], getAsyncDelegations(), doneAtMs + 1_000).rows).toHaveLength(1)
    expect(buildAgentRows([], getAsyncDelegations(), doneAtMs + DONE_LINGER_MS + 1_000).rows).toHaveLength(0)
  })
})

// ── C. Feature invocation: completion → parse → dispatch ─────────────

describe('e2e steering — what the panel offers is what the composer can send', () => {
  it('offers completions sourced from the live stores, not from arguments', () => {
    setSubagents([liveSub()])
    applyAsyncList(asyncListPayload() as any)

    const items = steerCompletionsForInput('@')

    expect(items).not.toBeNull()
    expect(items!.map(i => i.meta)).toEqual([
      expect.stringContaining('live subagent'),
      expect.stringContaining('background')
    ])
  })

  it('round-trip: the exact text a completion inserts resolves back to that agent', () => {
    applyAsyncList(asyncListPayload() as any)

    const [item] = steerCompletionsForInput('@b')!

    expect(item.text).toMatch(/^@\S+ $/)

    const cmd = parseSteerCommand(`${item.text}check the retry budget`)

    expect(cmd).not.toBeNull()
    expect(resolveAsyncSteerTargetId(cmd!.token, getAsyncDelegations())).toBe('b7c2a3f1')
  })

  it('routes a background id to delegation.send with the body only', () => {
    applyAsyncList(asyncListPayload() as any)

    const { calls, gw } = makeGateway()
    const deps = makeSteerDeps(gw)

    expect(submitAsUser('@b7c2a3f1 check the retry budget', deps)).toBe(true)
    expect(calls).toEqual([
      { method: 'delegation.send', params: { delegation_id: 'b7c2a3f1', text: 'check the retry budget' } }
    ])
  })

  it('routes a live subagent id to subagent.send and wins over a background match', () => {
    setSubagents([liveSub({ id: 'dup1' })])
    applyAsyncList(
      asyncListPayload({ delegations: [{ ...asyncListPayload().delegations[0], delegation_id: 'dup1' }] }) as any
    )

    const { calls, gw } = makeGateway()

    expect(submitAsUser('@dup1 stop', makeSteerDeps(gw))).toBe(true)
    expect(calls[0].method).toBe('subagent.send')
    expect(calls).toHaveLength(1)
  })

  it('echoes the steered text into the transcript — the only record of it', () => {
    applyAsyncList(asyncListPayload() as any)

    const { gw } = makeGateway()
    const appendMessage = vi.fn()
    const clearIn = vi.fn()
    const pushHistory = vi.fn()

    submitAsUser('@b7c2a3f1 check the retry budget', makeSteerDeps(gw, { appendMessage, clearIn, pushHistory }))

    expect(appendMessage).toHaveBeenCalledWith({ role: 'user', text: '@b7c2a3f1 check the retry budget' })
    expect(clearIn).toHaveBeenCalled()
    expect(pushHistory).toHaveBeenCalledWith('@b7c2a3f1 check the retry budget')
  })

  it('confirms delivery in the system line', async () => {
    applyAsyncList(asyncListPayload() as any)

    const { gw } = makeGateway(() => ({ delivered: true }))
    const sys = vi.fn()

    submitAsUser('@b7c2a3f1 go', makeSteerDeps(gw, { sys }))
    await vi.waitFor(() => expect(sys).toHaveBeenCalledWith('delivered → @b7c2a3f1'))
  })

  it('reports a race with a finishing agent instead of claiming delivery', async () => {
    applyAsyncList(asyncListPayload() as any)

    const { gw } = makeGateway(() => ({ delivered: false }))
    const sys = vi.fn()

    submitAsUser('@b7c2a3f1 go', makeSteerDeps(gw, { sys }))
    await vi.waitFor(() => expect(sys).toHaveBeenCalledWith('@b7c2a3f1 already finished'))
  })

  it('reports a transport failure without losing the turn', async () => {
    applyAsyncList(asyncListPayload() as any)

    const { gw } = makeGateway(() => {
      throw new Error('socket closed')
    })

    const sys = vi.fn()

    expect(submitAsUser('@b7c2a3f1 go', makeSteerDeps(gw, { sys }))).toBe(true)
    await vi.waitFor(() => expect(sys).toHaveBeenCalledWith('steer failed — @b7c2a3f1 unreachable'))
  })

  it('a token that resolves to nothing falls through as an ordinary prompt', () => {
    applyAsyncList(asyncListPayload() as any)

    const { calls, gw } = makeGateway()
    const appendMessage = vi.fn()

    expect(submitAsUser('@john ping me about the retry budget', makeSteerDeps(gw, { appendMessage }))).toBe(false)
    expect(calls).toEqual([])
    expect(appendMessage).not.toHaveBeenCalled()
  })

  it('an ambiguous prefix is never guessed — it falls through too', () => {
    applyAsyncList(
      asyncListPayload({
        delegations: [
          { ...asyncListPayload().delegations[0], delegation_id: 'b7c2aaaa' },
          { ...asyncListPayload().delegations[0], delegation_id: 'b7c2bbbb' }
        ],
        running: 2
      }) as any
    )

    const { calls, gw } = makeGateway()

    expect(submitAsUser('@b7c2 stop', makeSteerDeps(gw))).toBe(false)
    expect(calls).toEqual([])
  })

  it('a finished delegation still in the panel is not steerable', () => {
    applyAsyncList(
      asyncListPayload({
        delegations: [{ ...asyncListPayload().delegations[0], completed_at: 1_060_000, status: 'completed' }],
        running: 0
      }) as any
    )

    const { calls, gw } = makeGateway()

    expect(submitAsUser('@b7c2a3f1 more', makeSteerDeps(gw))).toBe(false)
    expect(calls).toEqual([])
  })
})

// ── D. UX / design ───────────────────────────────────────────────────

describe('UX — the panel advertises steering only when it would work', () => {
  it('shows the @id hint while something is running and there is room', () => {
    applyAsyncList(asyncListPayload() as any)

    expect(renderFrame(<LiveAgentsPanel cols={100} />)[0]).toContain('@id steer')
  })

  it('drops the hint when nothing is running (nothing to steer)', () => {
    applyAsyncList(
      asyncListPayload({
        delegations: [
          { ...asyncListPayload().delegations[0], completed_at: Math.floor(Date.now() / 1000), status: 'completed' }
        ],
        running: 0
      }) as any
    )

    const frame = renderFrame(<LiveAgentsPanel cols={100} />)

    expect(frame[0]).toContain('agents')
    expect(frame[0]).not.toContain('@id steer')
  })

  it('drops both hints before it lets the counts wrap on a narrow terminal', () => {
    applyAsyncList(asyncListPayload() as any)

    const frame = renderFrame(<LiveAgentsPanel cols={34} />)

    expect(frame[0]).toContain('1 running')
    expect(frame[0]).not.toContain('@id steer')
    expect(frame[0].length).toBeLessThanOrEqual(34)
  })

  it('collapsing hides the rows but keeps the header and its counts', () => {
    setSubagents([liveSub()])
    applyAsyncList(asyncListPayload() as any)

    const open = renderFrame(<LiveAgentsPanel cols={80} />)

    patchTurnState({ agentsCollapsed: true })

    const shut = renderFrame(<LiveAgentsPanel cols={80} />)

    expect(open.length).toBeGreaterThan(shut.length)
    expect(shut).toHaveLength(1)
    expect(shut[0]).toContain('2 running')
    expect(shut[0]).toContain('▸')
  })

  it('documents the steer shorthand in the hotkey help', () => {
    expect(HOTKEYS.some(([key]) => key === '@<id> <text>')).toBe(true)
  })

  it('the panel prints the same abbreviated id the user is told to type', () => {
    applyAsyncList(asyncListPayload() as any)

    const [item] = steerCompletionsForInput('@')!
    const printed = item.display.slice(1)

    expect(renderFrame(<LiveAgentsPanel cols={80} />).join('\n')).toContain(printed)
  })
})

// ── E. Regression: the rest of the composer is untouched ─────────────

describe('regression — steering does not hijack the other input modes', () => {
  it('leaves slash commands to the gateway completer', () => {
    applyAsyncList(asyncListPayload() as any)

    expect(steerCompletionsForInput('/age')).toBeNull()
    expect(completionRequestForInput('/age')?.method).toBe('complete.slash')
  })

  it('leaves path words to the gateway completer', () => {
    applyAsyncList(asyncListPayload() as any)

    expect(steerCompletionsForInput('open src/lib/ag')).toBeNull()
    expect(completionRequestForInput('open src/lib/ag')?.method).toBe('complete.path')
  })

  it('suppresses the path request while the cursor is still in the @id position', () => {
    expect(steerCompletionsForInput('@b7')).toEqual([])
  })

  it('stops offering ids once the steer body has started', () => {
    applyAsyncList(asyncListPayload() as any)

    expect(steerCompletionsForInput('@b7c2a3f1 check')).toBeNull()
  })

  it('an ordinary prompt containing @ is never parsed as a steer target', () => {
    applyAsyncList(asyncListPayload() as any)

    const { calls, gw } = makeGateway()

    expect(submitAsUser('email @john about the retry budget', makeSteerDeps(gw))).toBe(false)
    expect(calls).toEqual([])
  })

  it('resetting the async snapshot clears the panel', () => {
    applyAsyncList(asyncListPayload() as any)
    expect(renderFrame(<LiveAgentsPanel cols={72} />)).not.toEqual([])

    resetAsyncDelegations()
    expect(renderFrame(<LiveAgentsPanel cols={72} />)).toEqual([])
  })

  it('a session with only live subagents still paints (async list never fetched)', () => {
    setSubagents([liveSub()])
    patchUiState({ sid: 'sess-1' })

    expect(renderFrame(<LiveAgentsPanel cols={80} />).join('\n')).toContain('1 running')
  })
})
