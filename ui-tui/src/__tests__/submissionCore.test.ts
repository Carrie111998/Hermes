import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  dispatchSteer,
  isSessionBusyError,
  markSubmitting,
  type SteerDispatchDeps,
  submitPrompt,
  type SubmitPromptDeps
} from '../app/submissionCore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import type { GatewayClient } from '../gatewayClient.js'

// A gateway double whose `input.detect_drop` resolution we control, so we can
// observe UI state DURING the async gap — the exact window the queue-mode race
// lived in.
function makeDeferredGateway() {
  let resolveDrop: (v: unknown) => void = () => {}

  const dropPromise = new Promise(res => {
    resolveDrop = res
  })

  const calls: string[] = []

  const gw = {
    request: vi.fn((method: string) => {
      calls.push(method)

      if (method === 'input.detect_drop') {
        return dropPromise
      }

      // prompt.submit et al: resolve immediately with a success shape.
      return Promise.resolve({ status: 'streaming' })
    })
  } as unknown as GatewayClient

  return { calls, gw, resolveDrop: (v: unknown = { matched: false }) => resolveDrop(v) }
}

function makeDeps(gw: GatewayClient, over: Partial<SubmitPromptDeps> = {}): SubmitPromptDeps {
  return {
    appendMessage: vi.fn(),
    enqueue: vi.fn(),
    expand: (t: string) => t,
    gw,
    setLastUserMsg: vi.fn(),
    sys: vi.fn(),
    ...over
  }
}

describe('submissionCore.submitPrompt — synchronous busy (queue-race fix)', () => {
  beforeEach(() => {
    resetUiState()
    patchUiState({ sid: 'sess-1' })
  })

  it('flips busy=true SYNCHRONOUSLY, before input.detect_drop resolves', () => {
    const { gw, resolveDrop } = makeDeferredGateway()

    expect(getUiState().busy).toBe(false)

    submitPrompt('hello', makeDeps(gw))

    // The critical invariant: busy is already true even though the
    // detect_drop RPC has NOT resolved yet. This is what makes a second,
    // rapid submit take the local-enqueue branch instead of racing a second
    // prompt.submit onto the backend.
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('running…')

    resolveDrop()
  })

  it('regression: two back-to-back sends — the SECOND sees busy=true in the gap', async () => {
    const { gw, resolveDrop } = makeDeferredGateway()

    // Emulate dispatchSubmission's routing decision: it sends only when
    // busy===false, otherwise it would enqueue. We assert the state the
    // router reads, which is the real regression.
    submitPrompt('first message', makeDeps(gw))

    // Before the fix, busy was still false here (set only inside detect_drop's
    // .then), so a second Enter would wrongly route into send() again.
    const busyWhenSecondArrives = getUiState().busy
    expect(busyWhenSecondArrives).toBe(true)

    resolveDrop()
    await Promise.resolve()
  })

  it('does not submit when there is no session, and does not mark busy', () => {
    resetUiState() // sid: null
    const { gw, calls } = makeDeferredGateway()
    const sys = vi.fn()

    submitPrompt('hello', makeDeps(gw, { sys }))

    expect(getUiState().busy).toBe(false)
    expect(sys).toHaveBeenCalledWith('session not ready yet')
    expect(calls).not.toContain('input.detect_drop')
  })

  it('after detect_drop resolves (no file), it issues prompt.submit', async () => {
    const { calls, gw, resolveDrop } = makeDeferredGateway()

    submitPrompt('hi there', makeDeps(gw))
    expect(calls).toEqual(['input.detect_drop'])

    resolveDrop({ matched: false })
    await Promise.resolve()
    await Promise.resolve()

    expect(calls).toContain('prompt.submit')
  })
})

// `@<id> text` never reaches prompt.submit, so dispatchSteer is the only place
// that can put the steered text in the transcript. Before this, the session
// showed `delivered → @b7c2` with no record of what was actually sent.
describe('submissionCore.dispatchSteer — transcript record of steered text', () => {
  function makeSteerDeps(request: ReturnType<typeof vi.fn>, over: Partial<SteerDispatchDeps> = {}): SteerDispatchDeps {
    return {
      appendMessage: vi.fn(),
      clearIn: vi.fn(),
      gw: { request } as unknown as GatewayClient,
      pushHistory: vi.fn(),
      sys: vi.fn(),
      ...over
    }
  }

  const settle = async () => {
    await Promise.resolve()
    await Promise.resolve()
  }

  it('echoes the steer body into the transcript for a live subagent', async () => {
    const request = vi.fn().mockResolvedValue({ delivered: true })
    const deps = makeSteerDeps(request)

    const handled = dispatchSteer(
      { body: 'check the retry budget', token: 'b7c2' },
      { subagentId: 'sub-b7c2-full' },
      '@b7c2 check the retry budget',
      deps
    )

    expect(handled).toBe(true)
    expect(deps.appendMessage).toHaveBeenCalledWith({ role: 'user', text: '@b7c2 check the retry budget' })
    expect(request).toHaveBeenCalledWith('subagent.send', {
      subagent_id: 'sub-b7c2-full',
      text: 'check the retry budget'
    })

    await settle()
    expect(deps.sys).toHaveBeenCalledWith('delivered → @b7c2')
  })

  it('routes a background delegation to delegation.send and still echoes', async () => {
    const request = vi.fn().mockResolvedValue({ delivered: true })
    const deps = makeSteerDeps(request)

    const handled = dispatchSteer(
      { body: 'stop after the current file', token: 'd41d' },
      { delegationId: 'del-d41d-full', subagentId: null },
      '@d41d stop after the current file',
      deps
    )

    expect(handled).toBe(true)
    expect(request).toHaveBeenCalledWith('delegation.send', {
      delegation_id: 'del-d41d-full',
      text: 'stop after the current file'
    })
    expect(deps.appendMessage).toHaveBeenCalledWith({ role: 'user', text: '@d41d stop after the current file' })

    await settle()
    expect(deps.sys).toHaveBeenCalledWith('delivered → @d41d')
  })

  it('prefers the live subagent when a token resolves on both sides', () => {
    const request = vi.fn().mockResolvedValue({ delivered: true })
    const deps = makeSteerDeps(request)

    dispatchSteer({ body: 'ping', token: 'aa11' }, { delegationId: 'del-aa11', subagentId: 'sub-aa11' }, 'x', deps)

    expect(request).toHaveBeenCalledWith('subagent.send', { subagent_id: 'sub-aa11', text: 'ping' })
  })

  it('keeps the echo but reports when the target already finished', async () => {
    const request = vi.fn().mockResolvedValue({ delivered: false })
    const deps = makeSteerDeps(request)

    dispatchSteer({ body: 'too late', token: 'b7c2' }, { subagentId: 'sub-b7c2' }, '@b7c2 too late', deps)

    await settle()
    expect(deps.appendMessage).toHaveBeenCalledWith({ role: 'user', text: '@b7c2 too late' })
    expect(deps.sys).toHaveBeenCalledWith('@b7c2 already finished')
  })

  it('reports an unreachable target when the RPC rejects', async () => {
    const request = vi.fn().mockRejectedValue(new Error('closed'))
    const deps = makeSteerDeps(request)

    dispatchSteer({ body: 'hello', token: 'b7c2' }, { subagentId: 'sub-b7c2' }, '@b7c2 hello', deps)

    await settle()
    expect(deps.sys).toHaveBeenCalledWith('steer failed — @b7c2 unreachable')
  })

  it('returns false with NO side effects when the token matches nothing', () => {
    const request = vi.fn()
    const deps = makeSteerDeps(request)

    // "@john ping me" must fall through to an ordinary prompt: no echo, no
    // history push, and the composer must keep its text for submitPrompt.
    const handled = dispatchSteer({ body: 'ping me', token: 'john' }, {}, '@john ping me', deps)

    expect(handled).toBe(false)
    expect(request).not.toHaveBeenCalled()
    expect(deps.appendMessage).not.toHaveBeenCalled()
    expect(deps.pushHistory).not.toHaveBeenCalled()
    expect(deps.clearIn).not.toHaveBeenCalled()
  })

  it('pushes the expanded history entry and clears the composer before sending', () => {
    const request = vi.fn().mockResolvedValue({ delivered: true })
    const deps = makeSteerDeps(request)

    dispatchSteer({ body: 'see [[paste]]', token: 'b7c2' }, { subagentId: 'sub-b7c2' }, '@b7c2 see EXPANDED', deps)

    expect(deps.pushHistory).toHaveBeenCalledWith('@b7c2 see EXPANDED')
    expect(deps.clearIn).toHaveBeenCalled()
  })
})

describe('submissionCore.markSubmitting', () => {
  beforeEach(() => resetUiState())

  it('sets busy + running status', () => {
    markSubmitting()
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('running…')
  })
})

describe('submissionCore.isSessionBusyError', () => {
  it('matches the legacy busy rejections but not arbitrary errors', () => {
    expect(isSessionBusyError(new Error('session busy'))).toBe(true)
    expect(isSessionBusyError(new Error('waiting for model response'))).toBe(true)
    expect(isSessionBusyError(new Error('some other failure'))).toBe(false)
    expect(isSessionBusyError('not an error')).toBe(false)
  })
})
