import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { $gateway } from '@/store/gateway'

import { useGatewayRequest } from './use-gateway-request'

const fakeGateway = { connectionState: 'open' } as unknown as HermesGateway
const initialDesktop = window.hermesDesktop

afterEach(() => {
  $gateway.set(null)
  window.hermesDesktop = initialDesktop
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

  it('retires trusted provenance only after prompt acceptance', async () => {
    const request = vi.fn().mockResolvedValue({ status: 'streaming' })

    const mintDirectActionPrompt = vi.fn().mockResolvedValue({
      payload: {
        version: 2,
        event_id: 'event-1',
        observed_at: '2026-07-21T15:00:00.000Z',
        installation_id: 'install-1',
        os_account: 'darwin:501',
        app_identity: 'TEAM:io.hermes.desktop',
        app_instance_id: 'instance-1',
        window_id: '7',
        text_hash: 'a'.repeat(64)
      },
      public_key_fingerprint: 'b'.repeat(64),
      signature: 'signed'
    })

    const retireDirectActionPrompt = vi.fn().mockResolvedValue(true)

    window.hermesDesktop = {
      mintDirectActionPrompt,
      retireDirectActionPrompt
    } as unknown as Window['hermesDesktop']
    $gateway.set({ connectionState: 'open', request } as unknown as HermesGateway)
    const { result } = renderHook(() => useGatewayRequest())

    await act(async () => {
      await result.current.requestGateway('prompt.submit', {
        session_id: 'runtime-1',
        text: 'log duty'
      })
    })

    expect(request.mock.calls[0][1].desktop_provenance.payload.event_id).toBe(
      'event-1'
    )
    expect(retireDirectActionPrompt).toHaveBeenCalledWith('event-1')
  })

  it('reuses one event across session recovery and keeps failures recoverable', async () => {
    const request = vi
      .fn()
      .mockRejectedValueOnce(new Error('session not found'))
      .mockResolvedValueOnce({ status: 'streaming' })

    const provenance = {
      payload: {
        version: 2 as const,
        event_id: 'event-recovery',
        observed_at: '2026-07-21T15:00:00.000Z',
        installation_id: 'install-1',
        os_account: 'darwin:501',
        app_identity: 'TEAM:io.hermes.desktop',
        app_instance_id: 'instance-1',
        window_id: '7',
        text_hash: 'a'.repeat(64)
      },
      public_key_fingerprint: 'b'.repeat(64),
      signature: 'signed'
    }

    const mintDirectActionPrompt = vi.fn().mockResolvedValue(provenance)
    const retireDirectActionPrompt = vi.fn().mockResolvedValue(true)

    window.hermesDesktop = {
      mintDirectActionPrompt,
      retireDirectActionPrompt
    } as unknown as Window['hermesDesktop']
    $gateway.set({ connectionState: 'open', request } as unknown as HermesGateway)
    const { result } = renderHook(() => useGatewayRequest())

    await expect(
      result.current.requestGateway('prompt.submit', {
        session_id: 'runtime-old',
        text: 'log duty'
      })
    ).rejects.toThrow('session not found')
    expect(retireDirectActionPrompt).not.toHaveBeenCalled()

    await result.current.requestGateway('prompt.submit', {
      session_id: 'runtime-new',
      text: 'log duty'
    })

    expect(mintDirectActionPrompt).toHaveBeenCalledTimes(2)
    expect(
      request.mock.calls.map(call => call[1].desktop_provenance.payload.event_id)
    ).toEqual(['event-recovery', 'event-recovery'])
    expect(retireDirectActionPrompt).toHaveBeenCalledWith('event-recovery')
  })
})
