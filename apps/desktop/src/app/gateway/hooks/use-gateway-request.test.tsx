// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { $gateway } from '@/store/gateway'
import { $gatewayState } from '@/store/session'

import { useGatewayRequest } from './use-gateway-request'

describe('useGatewayRequest', () => {
  beforeEach(() => {
    $gateway.set(null)
    $gatewayState.set('idle')
  })

  afterEach(() => {
    cleanup()
    delete (window as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('reconnects the live gateway when the UI state is stale-open', async () => {
    let transportState: 'closed' | 'open' = 'closed'
    let requestCount = 0

    const gateway = {
      get connectionState() {
        return transportState
      },
      connect: vi.fn(async () => {
        transportState = 'open'
      }),
      request: vi.fn(async () => {
        requestCount += 1

        if (transportState !== 'open') {
          throw new Error('evaOS Agent gateway is not connected')
        }

        return { ok: true }
      })
    } as unknown as HermesGateway

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
      getConnection: vi.fn(async () => ({
        authMode: 'oauth',
        baseUrl: 'https://agent.example',
        profile: 'default',
        token: '',
        wsUrl: 'wss://agent.example/api/ws?ticket=stale'
      })),
      getGatewayWsUrl: vi.fn(async () => 'wss://agent.example/api/ws?ticket=fresh')
    }

    $gateway.set(gateway)
    // Reproduce the customer screenshot: the status bar still says ready even
    // though the socket used by prompt.submit has already closed.
    $gatewayState.set('open')

    const { result } = renderHook(() => useGatewayRequest())

    await act(async () => {
      await expect(result.current.requestGateway('prompt.submit', { text: 'hello' })).resolves.toEqual({ ok: true })
    })

    expect(requestCount).toBe(2)
    expect(gateway.connect).toHaveBeenCalledWith('wss://agent.example/api/ws?ticket=fresh')
  })

  it('forces reconnect when a failed request races the live state transition', async () => {
    let connected = false
    let requestCount = 0

    const gateway = {
      // A real WebSocket close event can trail the request rejection by one
      // task, so the public state may still report open in the catch block.
      get connectionState() {
        return 'open' as const
      },
      connect: vi.fn(async () => {
        connected = true
      }),
      request: vi.fn(async () => {
        requestCount += 1

        if (!connected) {
          throw new Error('evaOS Agent gateway is not connected')
        }

        return { ok: true }
      })
    } as unknown as HermesGateway

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
      getConnection: vi.fn(async () => ({
        authMode: 'oauth',
        baseUrl: 'https://agent.example',
        profile: 'default',
        token: '',
        wsUrl: 'wss://agent.example/api/ws?ticket=stale'
      })),
      getGatewayWsUrl: vi.fn(async () => 'wss://agent.example/api/ws?ticket=fresh')
    }

    $gateway.set(gateway)
    $gatewayState.set('open')

    const { result } = renderHook(() => useGatewayRequest())

    await act(async () => {
      await expect(result.current.requestGateway('prompt.submit', { text: 'hello' })).resolves.toEqual({ ok: true })
    })

    expect(requestCount).toBe(2)
    expect(gateway.connect).toHaveBeenCalledWith('wss://agent.example/api/ws?ticket=fresh')
  })

  it('waits for an in-flight boot reconnect before retrying the failed request', async () => {
    let transportState: 'connecting' | 'open' = 'connecting'
    let requestCount = 0
    const stateHandlers = new Set<(state: 'connecting' | 'open') => void>()

    const gateway = {
      get connectionState() {
        return transportState
      },
      connect: vi.fn(async () => {
        // Match JsonRpcGatewayClient.connect(): an existing "connecting"
        // attempt returns without waiting for the socket open event.
        window.setTimeout(() => {
          transportState = 'open'

          for (const handler of stateHandlers) {
            handler(transportState)
          }
        }, 10)
      }),
      onState: vi.fn((handler: (state: 'connecting' | 'open') => void) => {
        stateHandlers.add(handler)
        handler(transportState)

        return () => stateHandlers.delete(handler)
      }),
      request: vi.fn(async () => {
        requestCount += 1

        if (transportState !== 'open') {
          throw new Error('evaOS Agent gateway is not connected')
        }

        return { ok: true }
      })
    } as unknown as HermesGateway

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
      getConnection: vi.fn(async () => ({
        authMode: 'oauth',
        baseUrl: 'https://agent.example',
        profile: 'default',
        token: '',
        wsUrl: 'wss://agent.example/api/ws?ticket=stale'
      })),
      getGatewayWsUrl: vi.fn(async () => 'wss://agent.example/api/ws?ticket=fresh')
    }

    $gateway.set(gateway)
    $gatewayState.set('open')

    const { result } = renderHook(() => useGatewayRequest())

    await act(async () => {
      await expect(result.current.requestGateway('prompt.submit', { text: 'hello' })).resolves.toEqual({ ok: true })
    })

    expect(requestCount).toBe(2)
    expect(stateHandlers.size).toBe(0)
  })
})
