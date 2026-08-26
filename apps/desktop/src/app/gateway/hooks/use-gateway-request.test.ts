import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { $gateway } from '@/store/gateway'

import { useGatewayRequest } from './use-gateway-request'

const fakeGateway = { connectionState: 'open' } as unknown as HermesGateway

afterEach(() => {
  $gateway.set(null)
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  vi.useRealTimers()
})

describe('useGatewayRequest', () => {
  // The composer's `/` completions only exist when ChatBar receives a non-null
  // gateway PROP. `gatewayRef` is populated by a subscription effect, so it is
  // still null on the first render — a surface that read the ref while
  // rendering (session tiles / ⌘T tabs) shipped `gateway={null}` and silently
  // lost slash completions. The returned `gateway` value must be live
  // immediately so that never happens again.
  it('exposes the live gateway on the first render, before effects run', () => {
    $gateway.set(fakeGateway)

    const { result } = renderHook(() => useGatewayRequest())

    expect(result.current.gateway).toBe(fakeGateway)
  })

  it('tracks the gateway when the active socket changes', () => {
    const { result } = renderHook(() => useGatewayRequest())

    expect(result.current.gateway).toBeNull()

    act(() => $gateway.set(fakeGateway))

    expect(result.current.gateway).toBe(fakeGateway)
  })

  it('rejects instead of hanging forever when the reconnect getConnection() wedges (#93454)', async () => {
    // Repro: a request lands on a dropped socket, the "not connected" catch
    // kicks off a reconnect, and the IPC round-trip into main
    // (desktop.getConnection) never settles — e.g. a wedged revalidation after
    // a liveness-probe trip. Without an internal timeout on that await,
    // reconnectingRef never clears and requestGateway hangs forever instead of
    // surfacing the original transport error.
    vi.useFakeTimers()

    const dropped = {
      connectionState: 'closed',
      request: vi.fn().mockRejectedValue(new Error('connection closed'))
    } as unknown as HermesGateway
    const getConnection = vi.fn(() => new Promise(() => undefined))

    ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = { getConnection }
    $gateway.set(dropped)

    const { result } = renderHook(() => useGatewayRequest())

    const pending = expect(result.current.requestGateway('some.method')).rejects.toThrow('connection closed')

    // Advance past the internal reconnect-attempt timeout (20s) — the stalled
    // getConnection() await must reject so the reconnect gives up and the
    // original transport error surfaces, instead of requestGateway() never
    // settling.
    await vi.advanceTimersByTimeAsync(20_000)
    await pending
  })
})
