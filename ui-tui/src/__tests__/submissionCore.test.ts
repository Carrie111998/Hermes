import { beforeEach, describe, expect, it, vi } from 'vitest'

import { isSessionBusyError, markSubmitting, submitPrompt, type SubmitPromptDeps } from '../app/submissionCore.js'
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

  it('carries the detected-image reservation into its matching prompt.submit', async () => {
    const gw = {
      request: vi.fn((method: string) => {
        if (method === 'input.detect_drop') {
          return Promise.resolve({
            attachment_batch_id: 'reservation-a',
            is_image: true,
            matched: true,
            name: 'private.png',
            text: '[User attached image: private.png]'
          })
        }

        return Promise.resolve({ status: 'streaming' })
      })
    } as unknown as GatewayClient

    submitPrompt('/tmp/private.png', makeDeps(gw))
    await Promise.resolve()
    await Promise.resolve()

    expect(gw.request).toHaveBeenCalledWith('prompt.submit', {
      attachment_batch_id: 'reservation-a',
      session_id: 'sess-1',
      text: '[User attached image: private.png]'
    })
  })

  it('surfaces a SMART routing acknowledgement returned by prompt.submit', async () => {
    const sys = vi.fn()

    const gw = {
      request: vi.fn((method: string) => {
        if (method === 'input.detect_drop') {
          return Promise.resolve({ matched: false })
        }

        if (method === 'prompt.submit') {
          return Promise.resolve({
            status: 'smart_related',
            route: 'related',
            ack: 'SMART related; active mission continues.'
          })
        }

        return Promise.resolve({})
      })
    } as unknown as GatewayClient

    submitPrompt('same mission update', makeDeps(gw, { sys }))
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(sys).toHaveBeenCalledWith('SMART related; active mission continues.')
  })

  it('shows an uncertain durable steer without encouraging an automatic replay', async () => {
    const sys = vi.fn()

    const gw = {
      request: vi.fn((method: string) => {
        if (method === 'input.detect_drop') {
          return Promise.resolve({ matched: false })
        }

        return Promise.resolve({
          accepted: true,
          ack: 'Steer delivery is uncertain; payload preserved. Do not resend automatically.',
          status: 'smart_uncertain'
        })
      })
    } as unknown as GatewayClient

    submitPrompt('same mission update', makeDeps(gw, { sys }))
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(sys).toHaveBeenCalledWith(
      'Steer delivery is uncertain; payload preserved. Do not resend automatically.'
    )
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('recovery required — do not resend')
  })

  it('shows an explicit non-acceptance state when the bounded queue rejects', async () => {
    const sys = vi.fn()

    const gw = {
      request: vi.fn((method: string) => {
        if (method === 'input.detect_drop') {
          return Promise.resolve({ matched: false })
        }

        if (method === 'prompt.submit') {
          return Promise.resolve({
            accepted: false,
            ack: 'Message was not accepted: safe queue limit reached.',
            reason: 'max_items',
            status: 'smart_rejected'
          })
        }

        return Promise.resolve({})
      })
    } as unknown as GatewayClient

    submitPrompt('retry me later', makeDeps(gw, { sys }))
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(sys).toHaveBeenCalledWith('Message was not accepted: safe queue limit reached.')
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('not accepted — queue limit reached')
  })

  it('shows compute-host fail-closed preservation as queued for isolated retry', async () => {
    const sys = vi.fn()

    const gw = {
      request: vi.fn((method: string) => {
        if (method === 'input.detect_drop') {
          return Promise.resolve({ matched: false })
        }

        return Promise.resolve({
          accepted: true,
          ack: 'Compute host unavailable; prompt preserved for isolated retry.',
          status: 'queued_isolation_unavailable'
        })
      })
    } as unknown as GatewayClient

    submitPrompt('isolate this', makeDeps(gw, { sys }))
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(sys).toHaveBeenCalledWith('Compute host unavailable; prompt preserved for isolated retry.')
    expect(getUiState().busy).toBe(true)
    expect(getUiState().status).toBe('queued for isolated retry')
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
