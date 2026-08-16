import { describe, expect, it, vi } from 'vitest'

import {
  applyConnectionChange,
  commitConnectionFailure,
  resolveTerminalConnection,
  terminalRegistrySourceKind
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
})

describe('resolveTerminalConnection', () => {
  it('joins the exact registry backend before resolving its SSH terminal target', async () => {
    const identity = { connectionId: 'homelab', profile: 'venture' }
    const target = { ssh: {}, scope: 'conn:homelab::venture' }
    const getTarget = vi.fn().mockReturnValueOnce('pending').mockReturnValueOnce(target)
    const ensureBackend = vi.fn(async () => undefined)

    await expect(resolveTerminalConnection(identity, getTarget, ensureBackend)).resolves.toBe(target)
    expect(getTarget).toHaveBeenNthCalledWith(1, identity)
    expect(getTarget).toHaveBeenNthCalledWith(2, identity)
    expect(ensureBackend).toHaveBeenCalledOnce()
    expect(ensureBackend).toHaveBeenCalledWith(identity)
  })

  it('keeps registry remote and cloud terminals on the local shell path', async () => {
    const identity = { connectionId: 'cloud-prod', profile: 'venture' }
    const getTarget = vi.fn(() => null)
    const ensureBackend = vi.fn(async () => undefined)

    await expect(resolveTerminalConnection(identity, getTarget, ensureBackend)).resolves.toBeNull()
    expect(getTarget).toHaveBeenCalledOnce()
    expect(getTarget).toHaveBeenCalledWith(identity)
    expect(ensureBackend).not.toHaveBeenCalled()
  })

  it('does not start a local terminal while configured SSH remains unavailable', async () => {
    await expect(
      resolveTerminalConnection(
        { connectionId: null, profile: 'venture' },
        () => 'pending',
        async () => undefined
      )
    ).rejects.toThrow('not ready')
  })
})

describe('terminalRegistrySourceKind', () => {
  const sources = [
    { id: 'homelab', kind: 'ssh' },
    { id: 'cloud-prod', kind: 'cloud' }
  ]

  it('keeps known SSH sources on the remote terminal path', () => {
    expect(terminalRegistrySourceKind('homelab', sources)).toBe('ssh')
  })

  it('distinguishes removed sources so they cannot silently spawn locally', () => {
    expect(terminalRegistrySourceKind('cloud-prod', sources)).toBe('local')
    expect(terminalRegistrySourceKind('removed-source', sources)).toBe('missing')
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
