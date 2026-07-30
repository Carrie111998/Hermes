import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $currentUsage } from '@/store/session'
import type { UsageStats } from '@/types/hermes'

import { usePrimaryUsageSync } from './use-statusbar-items'

type GatewayRequester = <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>

const BASELINE_USAGE: UsageStats = { calls: 0, input: 0, output: 0, total: 0 }

const usageFor = (session: string): UsageStats => ({
  calls: 12,
  context_max: 307_200,
  context_percent: 30,
  context_used: 92_000,
  cost_usd: 0.42,
  input: 80_000,
  output: 12_000,
  total: 92_000,
  // tag so we can tell which session's payload landed in the store
  ...(session === 'session-B' ? { context_used: 64_000 } : {})
})

beforeEach(() => {
  $currentUsage.set(BASELINE_USAGE)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('usePrimaryUsageSync', () => {
  it('fetches session.usage once on first mount and writes the result into $currentUsage', async () => {
    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.usage') {
        return usageFor('session-A') as never
      }

      return undefined as never
    }) as unknown as GatewayRequester

    renderHook(() => usePrimaryUsageSync('session-A', requestGateway))

    await waitFor(() => {
      expect(requestGateway).toHaveBeenCalledWith('session.usage', { session_id: 'session-A' })
    })

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect($currentUsage.get()).toEqual({ ...BASELINE_USAGE, ...usageFor('session-A') })
  })

  it('refetches when the primary session changes (A -> B), totalling two calls', async () => {
    const requestGateway = vi.fn(async (_method: string, params?: Record<string, unknown>) => {
      const sessionId = (params?.session_id as string) ?? ''

      return usageFor(sessionId) as never
    }) as unknown as GatewayRequester

    const { rerender } = renderHook(({ id }) => usePrimaryUsageSync(id, requestGateway), {
      initialProps: { id: 'session-A' }
    })

    await waitFor(() => expect(requestGateway).toHaveBeenCalledTimes(1))
    expect($currentUsage.get()).toEqual({ ...BASELINE_USAGE, ...usageFor('session-A') })

    rerender({ id: 'session-B' })

    await waitFor(() => {
      expect(requestGateway).toHaveBeenLastCalledWith('session.usage', { session_id: 'session-B' })
    })

    expect(requestGateway).toHaveBeenCalledTimes(2)
    expect($currentUsage.get()).toEqual({ ...BASELINE_USAGE, ...usageFor('session-B') })
  })

  it('does not refetch on re-render when the session is unchanged', async () => {
    const requestGateway = vi.fn(async () => usageFor('session-A') as never) as unknown as GatewayRequester

    const { rerender } = renderHook(({ id }) => usePrimaryUsageSync(id, requestGateway), {
      initialProps: { id: 'session-A' }
    })

    await waitFor(() => expect(requestGateway).toHaveBeenCalledTimes(1))

    // A brand-new requester identity re-runs the effect, but the session-id
    // guard suppresses a duplicate fetch.
    const requestGateway2 = vi.fn(async () => usageFor('session-A') as never) as unknown as GatewayRequester
    rerender({ id: 'session-A' })

    await act(async () => {
      await Promise.resolve()
    })

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(requestGateway2).not.toHaveBeenCalled()
  })

  it('skips the fetch when there is no active session', async () => {
    const requestGateway = vi.fn(async () => usageFor('session-A') as never) as unknown as GatewayRequester

    renderHook(() => usePrimaryUsageSync(null, requestGateway))

    await act(async () => {
      await Promise.resolve()
    })

    expect(requestGateway).not.toHaveBeenCalled()
    expect($currentUsage.get()).toEqual(BASELINE_USAGE)
  })

  it('leaves $currentUsage untouched when the usage fetch rejects', async () => {
    const requestGateway = vi.fn(async () => {
      throw new Error('rpc unavailable')
    }) as unknown as GatewayRequester

    renderHook(() => usePrimaryUsageSync('session-A', requestGateway))

    await act(async () => {
      await Promise.resolve()
    })

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect($currentUsage.get()).toEqual(BASELINE_USAGE)
  })
})
