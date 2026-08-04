import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestProfile } from '@/hermes'

import { resolveSpeakStreamUrl } from './voice-playback'

afterEach(() => {
  setApiRequestProfile(null)
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

it('accepts only the exact managed speech-stream endpoint', async () => {
  const getConnection = vi.fn(async (profile: null | string) => ({
    authMode: 'token',
    profile,
    wsUrl: 'ws://127.0.0.1:4123/api/ws?ticket=stale'
  }))
  const getGatewayWsUrl = vi
    .fn()
    .mockResolvedValueOnce('ws://127.0.0.1:4123/unexpected/api/audio/speak-stream?ticket=wrong')
    .mockResolvedValueOnce('ws://127.0.0.1:4123/api/audio/speak-stream?ticket=exact')
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = { getConnection, getGatewayWsUrl }
  setApiRequestProfile('research')

  await expect(resolveSpeakStreamUrl()).resolves.toBeNull()
  await expect(resolveSpeakStreamUrl()).resolves.toBe('ws://127.0.0.1:4123/api/audio/speak-stream?ticket=exact')
  expect(getGatewayWsUrl).toHaveBeenCalledWith('research', '/api/audio/speak-stream')
})
