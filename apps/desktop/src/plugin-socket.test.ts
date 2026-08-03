import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { pluginSocket, setApiRequestProfile } from './hermes'

class FakeWebSocket {
  static opened: FakeWebSocket[] = []

  onclose: null | (() => void) = null
  onmessage: null | ((event: { data: unknown }) => void) = null

  constructor(readonly url: string) {
    FakeWebSocket.opened.push(this)
  }

  close(): void {}
}

describe('pluginSocket', () => {
  const getConnection = vi.fn(async (profile: null | string) => ({
    authMode: 'token',
    baseUrl: 'eva-managed://customer-one',
    profile,
    token: '',
    wsUrl: 'ws://127.0.0.1:4123/api/ws?ticket=stale'
  }))

  const getGatewayWsUrl = vi.fn(
    async (_profile: null | string, endpointPath: string) => `ws://127.0.0.1:4123${endpointPath}&ticket=fresh`
  )

  beforeEach(() => {
    FakeWebSocket.opened = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
      getConnection,
      getGatewayWsUrl
    }
    getConnection.mockClear()
    getGatewayWsUrl.mockClear()
  })

  afterEach(() => {
    setApiRequestProfile(null)
    delete (window as { hermesDesktop?: unknown }).hermesDesktop
    vi.unstubAllGlobals()
  })

  it('mints the exact plugin endpoint for the active profile', async () => {
    setApiRequestProfile('research')
    const dispose = pluginSocket('kanban', '/events?mode=live', vi.fn())

    await vi.waitFor(() => expect(FakeWebSocket.opened).toHaveLength(1))
    expect(getConnection).toHaveBeenCalledWith('research')
    expect(getGatewayWsUrl).toHaveBeenCalledWith('research', '/api/plugins/kanban/events?mode=live')
    expect(FakeWebSocket.opened[0].url).toBe('ws://127.0.0.1:4123/api/plugins/kanban/events?mode=live&ticket=fresh')
    dispose()
  })

  it('rejects plugin ids that could escape their namespace', () => {
    expect(() => pluginSocket('../other', '/events', vi.fn())).toThrow(/invalid plugin id/)
    expect(getGatewayWsUrl).not.toHaveBeenCalled()
  })
})
