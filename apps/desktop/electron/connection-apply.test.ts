import { describe, expect, it, vi } from 'vitest'

import { applyConnectionChange, commitConnectionFailure, resolveTerminalConnection } from './connection-apply'

function deferred() {
  let resolve!: () => void

  const promise = new Promise<void>(done => {
    resolve = done
  })

  return { promise, resolve }
}

describe('applyConnectionChange', () => {
  it.each([['SSH A to SSH B'], ['SSH to Cloud'], ['Cloud to SSH']])(
    'serializes %s behind bootstrap rollback before teardown and apply',
    async () => {
      const gate = deferred()
      const events: string[] = []

      const run = applyConnectionChange({
        cancelAndWait: async () => {
          events.push('cancel')
          await gate.promise
          events.push('drained')
        },
        isPrimary: true,
        scope: '',
        sendApplied: () => events.push('applied'),
        stopPool: vi.fn(),
        teardownPrimary: async () => {
          events.push('primary')
        },
        teardownSsh: async () => {
          events.push('ssh')
        }
      })

      await Promise.resolve()
      expect(events).toEqual(['cancel'])
      gate.resolve()
      await run
      expect(events).toEqual(['cancel', 'drained', 'ssh', 'primary', 'applied'])
    }
  )

  it('tears down only a non-primary scope without applying the primary connection', async () => {
    const events: string[] = []
    await applyConnectionChange({
      cancelAndWait: async scope => {
        events.push(`cancel:${scope}`)
      },
      isPrimary: false,
      scope: 'worker',
      sendApplied: () => events.push('applied'),
      stopPool: scope => events.push(`pool:${scope}`),
      teardownPrimary: async () => {
        events.push('primary')
      },
      teardownSsh: async scope => {
        events.push(`ssh:${scope}`)
      }
    })
    expect(events).toEqual(['cancel:worker', 'ssh:worker', 'pool:worker'])
  })

  it('dedupes two concurrent primary-scope calls into one teardown + apply', async () => {
    // Reproduces the Settings "Apply" button and the cloud-agent "Connect"
    // button firing back-to-back: each is an independent UI trigger with its
    // own pending-state guard, so nothing stops both calling
    // applyConnectionChange for the primary scope close together. Without
    // dedup, a second call starts its own teardownPrimary() while the first
    // is still waiting on the real process exit.
    const gate = deferred()
    const teardownPrimary = vi.fn(async () => {
      await gate.promise
    })
    const sendApplied = vi.fn()

    const first = applyConnectionChange({
      cancelAndWait: vi.fn(async () => undefined),
      isPrimary: true,
      scope: '',
      sendApplied,
      stopPool: vi.fn(),
      teardownPrimary,
      teardownSsh: vi.fn(async () => undefined)
    })

    // Flush enough microtasks for `first` to reach the in-flight teardown
    // and suspend on the still-open gate, without resolving it.
    for (let i = 0; i < 10; i++) {
      await Promise.resolve()
    }
    expect(teardownPrimary).toHaveBeenCalledOnce()

    const second = applyConnectionChange({
      cancelAndWait: vi.fn(async () => undefined),
      isPrimary: true,
      scope: '',
      sendApplied,
      stopPool: vi.fn(),
      teardownPrimary,
      teardownSsh: vi.fn(async () => undefined)
    })

    for (let i = 0; i < 10; i++) {
      await Promise.resolve()
    }
    // The second call joined the first's in-flight re-home instead of
    // starting its own teardown.
    expect(teardownPrimary).toHaveBeenCalledOnce()
    expect(sendApplied).not.toHaveBeenCalled()

    gate.resolve()
    await Promise.all([first, second])

    expect(teardownPrimary).toHaveBeenCalledOnce()
    expect(sendApplied).toHaveBeenCalledOnce()
  })
})

describe('resolveTerminalConnection', () => {
  it('joins an in-flight backend before resolving the SSH terminal target', async () => {
    const target = { ssh: {}, scope: '' }
    const getTarget = vi.fn().mockReturnValueOnce('pending').mockReturnValueOnce(target)
    const ensureBackend = vi.fn(async () => undefined)

    await expect(resolveTerminalConnection(getTarget, ensureBackend)).resolves.toBe(target)
    expect(ensureBackend).toHaveBeenCalledOnce()
  })

  it('does not start a local terminal while configured SSH remains unavailable', async () => {
    await expect(
      resolveTerminalConnection(
        () => 'pending',
        async () => undefined
      )
    ).rejects.toThrow('not ready')
  })
})

describe('commitConnectionFailure', () => {
  it('prevents a stale bootstrap from publishing failure state', () => {
    const stale = Promise.resolve('stale')
    const current = Promise.resolve('current')
    const commit = vi.fn()

    expect(commitConnectionFailure(current, stale, commit)).toBe(false)
    expect(commit).not.toHaveBeenCalled()
    expect(commitConnectionFailure(current, current, commit)).toBe(true)
    expect(commit).toHaveBeenCalledOnce()
  })
})
