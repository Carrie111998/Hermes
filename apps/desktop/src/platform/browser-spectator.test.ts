import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  assertSpectatorReadRequest,
  browserSpectatorApi,
  browserSpectatorConnection
} from './browser-spectator'

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json' },
    status
  })

describe('browser spectator transport', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    window.history.replaceState({}, '', '/')
    window.__HERMES_BASE_PATH__ = ''
    window.__HERMES_SESSION_TOKEN__ = undefined
    window.__HERMES_AUTH_REQUIRED__ = false
    window.__HERMES_SPECTATOR__ = true
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('permits audited GET endpoints and scopes the profile', async () => {
    window.__HERMES_SESSION_TOKEN__ = 'loopback-secret'
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sessions: [] }))

    await expect(
      browserSpectatorApi({ path: '/api/sessions?limit=40', profile: 'hermes2' })
    ).resolves.toEqual({ sessions: [] })

    expect(fetch).toHaveBeenCalledWith(
      '/api/sessions?limit=40&profile=hermes2',
      expect.objectContaining({
        credentials: 'include',
        method: 'GET',
        headers: expect.any(Headers)
      })
    )
    const headers = vi.mocked(fetch).mock.calls[0][1]?.headers as Headers
    expect(headers.get('X-Hermes-Session-Token')).toBe('loopback-secret')
  })

  it.each([
    { path: '/api/sessions/x', method: 'DELETE' },
    { path: '/api/sessions/x', method: 'PATCH', body: { title: 'changed' } },
    { path: '/api/config', method: 'POST', body: {} },
    { path: '/api/fs/read-text' },
    { path: '/api/pty' },
    { path: 'https://attacker.example/api/sessions' },
    { path: '/api/sessions', connectionId: 'other-host' }
  ])('rejects mutation or non-audited request %#', request => {
    expect(() => assertSpectatorReadRequest(request)).toThrow()
  })

  it('mints a fresh single-use websocket ticket in authenticated mode', async () => {
    window.__HERMES_AUTH_REQUIRED__ = true
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ ticket: 'one-shot' }))

    const connection = await browserSpectatorConnection()

    expect(fetch).toHaveBeenCalledWith('/api/auth/ws-ticket', {
      credentials: 'include',
      method: 'POST'
    })
    expect(connection.wsUrl).toBe('ws://localhost:3000/api/ws?ticket=one-shot')
    expect(connection.authMode).toBe('oauth')
    expect(connection.mode).toBe('remote')
  })

  it('uses the injected loopback token without placing it in REST storage', async () => {
    window.__HERMES_SESSION_TOKEN__ = 'ephemeral'

    const connection = await browserSpectatorConnection()

    expect(fetch).not.toHaveBeenCalled()
    expect(connection.wsUrl).toBe('ws://localhost:3000/api/ws?token=ephemeral')
    expect(connection.token).toBe('')
  })

  it('fails closed when neither authenticated mode nor a loopback token is available', async () => {
    await expect(browserSpectatorConnection()).rejects.toThrow('session token is unavailable')
  })
})
