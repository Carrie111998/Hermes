import { describe, expect, it, vi } from 'vitest'

import {
  applyConnectionChange,
  commitConnectionFailure,
  resolveTerminalConnection,
  terminalProfileForTarget
} from './connection-apply'

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
        stopProfilePool: vi.fn(),
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
      stopProfilePool: scope => events.push(`pool:${scope}`),
      teardownPrimary: async () => {
        events.push('primary')
      },
      teardownSsh: async scope => {
        events.push(`ssh:${scope}`)
      }
    })
    expect(events).toEqual(['cancel:worker', 'ssh:worker', 'pool:worker'])
  })

  it('does not finish a profile apply until every profile pool backend is stopped', async () => {
    const gate = deferred()
    const events: string[] = []
    let completed = false

    const run = applyConnectionChange({
      cancelAndWait: async () => {
        events.push('cancel')
      },
      isPrimary: false,
      scope: 'worker',
      sendApplied: vi.fn(),
      stopProfilePool: async () => {
        events.push('pool')
        await gate.promise
        events.push('pool-stopped')
      },
      teardownPrimary: vi.fn(),
      teardownSsh: async () => {
        events.push('ssh')
      }
    }).then(() => {
      completed = true
    })

    await vi.waitFor(() => expect(events).toEqual(['cancel', 'ssh', 'pool']))
    expect(completed).toBe(false)

    gate.resolve()
    await run
    expect(events).toEqual(['cancel', 'ssh', 'pool', 'pool-stopped'])
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

describe('terminalProfileForTarget', () => {
  it('uses the sender-bound configured profile and disables SSH for forced-local targets', () => {
    expect(terminalProfileForTarget({ kind: 'primary' }, 'default')).toBe('default')
    expect(terminalProfileForTarget({ kind: 'configured-profile', profile: 'worker' }, 'default')).toBe('worker')
    expect(terminalProfileForTarget({ kind: 'forced-local-profile', profile: 'coder' }, 'default')).toBe(null)
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
