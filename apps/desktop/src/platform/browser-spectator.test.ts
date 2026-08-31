import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  applySpectatorDocumentMode,
  assertSpectatorReadRequest,
  browserSpectatorApi,
  browserSpectatorConnection,
  SPECTATOR_ROOT_ATTRIBUTE
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
    window.__HERMES_SPECTATOR_TOKEN__ = undefined
    window.__HERMES_AUTH_REQUIRED__ = false
    window.__HERMES_SPECTATOR__ = true
  })

  afterEach(() => {
    document.documentElement.removeAttribute(SPECTATOR_ROOT_ATTRIBUTE)
    vi.unstubAllGlobals()
  })

  it('marks only the explicit browser spectator document', () => {
    const desktop = window.hermesDesktop

    delete (window as { hermesDesktop?: typeof window.hermesDesktop }).hermesDesktop
    expect(applySpectatorDocumentMode()).toBe(true)
    expect(document.documentElement.hasAttribute(SPECTATOR_ROOT_ATTRIBUTE)).toBe(true)

    window.hermesDesktop = desktop ?? ({} as typeof window.hermesDesktop)
    expect(applySpectatorDocumentMode()).toBe(false)
    expect(document.documentElement.hasAttribute(SPECTATOR_ROOT_ATTRIBUTE)).toBe(false)
    window.hermesDesktop = desktop
  })

  it('permits audited GET endpoints and scopes the profile', async () => {
    window.__HERMES_SPECTATOR_TOKEN__ = 'read-only-secret'
    vi.mocked(fetch).mockResolvedValueOnce(jsonResponse({ sessions: [] }))

    await expect(browserSpectatorApi({ path: '/api/sessions?limit=40', profile: 'hermes2' })).resolves.toEqual({
      sessions: []
    })

    expect(fetch).toHaveBeenCalledWith(
      '/api/sessions?limit=40&profile=hermes2',
      expect.objectContaining({
        credentials: 'include',
        method: 'GET',
        headers: expect.any(Headers)
      })
    )
    const headers = vi.mocked(fetch).mock.calls[0][1]?.headers as Headers
    expect(headers.get('X-Hermes-Spectator-Token')).toBe('read-only-secret')
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

  it('uses the scoped spectator websocket credential in authenticated mode', async () => {
    window.__HERMES_AUTH_REQUIRED__ = true
    window.__HERMES_SPECTATOR_TOKEN__ = 'read-only'

    const connection = await browserSpectatorConnection()

    expect(fetch).not.toHaveBeenCalled()
    expect(connection.wsUrl).toBe('ws://localhost:3000/api/ws?spectator=read-only')
    expect(connection.authMode).toBe('oauth')
    expect(connection.mode).toBe('remote')
  })

  it('uses the scoped credential without placing it in connection storage', async () => {
    window.__HERMES_SPECTATOR_TOKEN__ = 'ephemeral'

    const connection = await browserSpectatorConnection()

    expect(fetch).not.toHaveBeenCalled()
    expect(connection.wsUrl).toBe('ws://localhost:3000/api/ws?spectator=ephemeral')
    expect(connection.token).toBe('')
  })

  it('fails closed when the scoped credential is unavailable', async () => {
    await expect(browserSpectatorConnection()).rejects.toThrow('Spectator credential is unavailable')
  })
})
