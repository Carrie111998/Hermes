import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  clearSingleFlightSessionResumeState,
  registerRecoveredRuntime,
  singleFlightSessionResume,
  takeRecoveredRuntime
} from './single-flight-resume'
import { resumeStoredRuntimeSession, SessionRecoveryAborted, withSessionNotFoundResume } from './utils'

afterEach(() => {
  clearSingleFlightSessionResumeState()
  vi.restoreAllMocks()
})

describe('singleFlightSessionResume', () => {
  it('two concurrent resume callers for the same stored id produce ONE session.resume RPC', async () => {
    const requestGateway = vi.fn(async (method: string) => {
      expect(method).toBe('session.resume')
      // Yield so both callers are in flight before either resolves.
      await new Promise(resolve => setTimeout(resolve, 10))

      return { session_id: 'rt-fresh' }
    })

    const deps = { requestGateway: requestGateway as never, resolveProfile: async () => undefined }

    const [a, b] = await Promise.all([
      resumeStoredRuntimeSession('stored-a', deps),
      resumeStoredRuntimeSession('stored-a', deps)
    ])

    expect(a).toBe('rt-fresh')
    expect(b).toBe('rt-fresh')
    expect(requestGateway).toHaveBeenCalledTimes(1)
  })

  it('different stored ids still resume independently', async () => {
    const requestGateway = vi.fn(async (_method: string, params?: Record<string, unknown>) => {
      await new Promise(resolve => setTimeout(resolve, 5))

      return { session_id: `rt-${String(params?.session_id)}` }
    })

    const deps = { requestGateway: requestGateway as never, resolveProfile: async () => undefined }

    const [a, b] = await Promise.all([
      resumeStoredRuntimeSession('stored-a', deps),
      resumeStoredRuntimeSession('stored-b', deps)
    ])

    expect(a).toBe('rt-stored-a')
    expect(b).toBe('rt-stored-b')
    expect(requestGateway).toHaveBeenCalledTimes(2)
  })

  it('same stored id resumes independently for different exact owner recovery keys', async () => {
    const requests: string[] = []

    const requestA = vi.fn(async () => {
      requests.push('owner-a')
      await new Promise(resolve => setTimeout(resolve, 10))

      return { session_id: 'rt-owner-a' }
    })

    const requestB = vi.fn(async () => {
      requests.push('owner-b')

      return { session_id: 'rt-owner-b' }
    })

    const [a, b] = await Promise.all([
      resumeStoredRuntimeSession('stored-shared', {
        recoveryKey: 'owner-a\0profile\0stored-shared',
        requestGateway: requestA as never,
        resolveProfile: async () => 'profile'
      }),
      resumeStoredRuntimeSession('stored-shared', {
        recoveryKey: 'owner-b\0profile\0stored-shared',
        requestGateway: requestB as never,
        resolveProfile: async () => 'profile'
      })
    ])

    expect(a).toBe('rt-owner-a')
    expect(b).toBe('rt-owner-b')
    expect(requests).toEqual(expect.arrayContaining(['owner-a', 'owner-b']))
    expect(requestA).toHaveBeenCalledOnce()
    expect(requestB).toHaveBeenCalledOnce()
  })

  it('a rejected flight is not cached: the next caller retries', async () => {
    const run = vi
      .fn<() => Promise<{ session_id: string }>>()
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce({ session_id: 'rt-second' })

    await expect(singleFlightSessionResume('stored-a', run)).rejects.toThrow('boom')
    await expect(singleFlightSessionResume('stored-a', run)).resolves.toEqual({ session_id: 'rt-second' })
    expect(run).toHaveBeenCalledTimes(2)
  })
})

describe('drift-abort recovered-runtime cache', () => {
  it('drift-abort does not strand the recovered runtime — it is registered in the cache', async () => {
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.resume') {
        return { session_id: 'rt-recovered' }
      }

      throw new Error('unexpected call')
    })

    const call = vi.fn(async (liveId: string) => {
      if (liveId === 'rt-dead') {
        throw new Error('session not found: rt-dead')
      }

      return 'ok'
    })

    await expect(
      withSessionNotFoundResume('rt-dead', 'stored-a', call, {
        requestGateway: requestGateway as never,
        resolveProfile: async () => undefined,
        driftReason: () => 'user switched away'
      })
    ).rejects.toThrow(SessionRecoveryAborted)

    // The freshly-minted runtime is NOT abandoned: the next action reuses it.
    expect(takeRecoveredRuntime('stored-a')).toBe('rt-recovered')
    // Take-semantics: consumed exactly once.
    expect(takeRecoveredRuntime('stored-a')).toBeUndefined()
  })

  it('a later non-drifted recovery adopts the cached runtime instead of resuming again', async () => {
    registerRecoveredRuntime('stored-a', 'rt-cached')

    const requestGateway = vi.fn(async () => {
      throw new Error('session.resume must not be called when a cached runtime exists')
    })

    const onRecovered = vi.fn()

    const call = vi.fn(async (liveId: string) => {
      if (liveId === 'rt-dead') {
        throw new Error('session not found: rt-dead')
      }

      return `ran-on-${liveId}`
    })

    const outcome = await withSessionNotFoundResume('rt-dead', 'stored-a', call, {
      requestGateway: requestGateway as never,
      resolveProfile: async () => undefined,
      onRecovered
    })

    expect(outcome).toEqual({ recovered: true, result: 'ran-on-rt-cached', sessionId: 'rt-cached' })
    expect(onRecovered).toHaveBeenCalledWith('rt-cached')
    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('takeRecoveredRuntime skips a cached id the caller already knows is dead', () => {
    registerRecoveredRuntime('stored-a', 'rt-dead')

    expect(takeRecoveredRuntime('stored-a', 'rt-dead')).toBeUndefined()
    expect(takeRecoveredRuntime('stored-a')).toBeUndefined()
  })

  it('does not reuse a drift-aborted recovered runtime from another exact owner', async () => {
    const deadThenLive = (deadId: string) => async (liveId: string) => {
      if (liveId === deadId) {
        throw new Error(`session not found: ${deadId}`)
      }

      return liveId
    }

    await expect(
      withSessionNotFoundResume('rt-dead-a', 'stored-shared', deadThenLive('rt-dead-a'), {
        driftReason: () => 'owner A navigated away',
        recoveryKey: 'owner-a\0profile\0stored-shared',
        requestGateway: vi.fn(async () => ({ session_id: 'rt-owner-a' })) as never,
        resolveProfile: async () => 'profile'
      })
    ).rejects.toThrow(SessionRecoveryAborted)

    const requestB = vi.fn(async () => ({ session_id: 'rt-owner-b' }))

    const outcome = await withSessionNotFoundResume('rt-dead-b', 'stored-shared', deadThenLive('rt-dead-b'), {
      recoveryKey: 'owner-b\0profile\0stored-shared',
      requestGateway: requestB as never,
      resolveProfile: async () => 'profile'
    })

    expect(outcome.sessionId).toBe('rt-owner-b')
    expect(requestB).toHaveBeenCalledOnce()
    expect(takeRecoveredRuntime('owner-a\0profile\0stored-shared')).toBe('rt-owner-a')
  })
})
