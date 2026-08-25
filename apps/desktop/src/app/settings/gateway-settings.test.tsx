import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $notifications } from '@/store/notifications'

const getConnectionConfig = vi.fn()
const saveConnectionConfig = vi.fn()
const oauthLoginConnectionConfig = vi.fn()
const probeConnectionConfig = vi.fn()

const localConnection = {
  cloudOrg: '',
  envOverride: false,
  mode: 'local',
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: false,
  remoteUrl: ''
}

beforeEach(() => {
  $notifications.set([])
  getConnectionConfig.mockResolvedValue(localConnection)
  saveConnectionConfig.mockResolvedValue(localConnection)
  probeConnectionConfig.mockResolvedValue({ providers: [] })
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { getConnectionConfig, oauthLoginConnectionConfig, probeConnectionConfig, saveConnectionConfig }
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('GatewaySettings', () => {
  it('loads the machine-level connection config (no profile scoping)', async () => {
    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)
    expect(await screen.findByText('Local gateway')).toBeTruthy()
    expect(
      screen.getByText('Start a private Hermes backend on localhost. This is the default and works offline.')
    ).toBeTruthy()

    // The page manages the machine's gateway connections; it must load the
    // global config, never a per-profile override.
    await waitFor(() => expect(getConnectionConfig).toHaveBeenCalledWith(null))
    expect(getConnectionConfig).not.toHaveBeenCalledWith(expect.any(String))

    // The legacy per-profile scope switcher must not render.
    expect(screen.queryByText('Applies to')).toBeNull()
    expect(screen.queryByText('All profiles')).toBeNull()
    expect(screen.queryByText('Use default gateway')).toBeNull()
  })

  it('retries a native failure through the embedded flow only from the notification action', async () => {
    const gatewayUrl = 'https://gateway.example.com/hermes'

    const remoteOauthConnection = {
      ...localConnection,
      mode: 'remote',
      remoteAuthMode: 'oauth',
      remoteUrl: gatewayUrl
    }

    getConnectionConfig.mockResolvedValue(remoteOauthConnection)
    saveConnectionConfig.mockResolvedValue(remoteOauthConnection)
    probeConnectionConfig.mockResolvedValue({
      authMode: 'oauth',
      baseUrl: gatewayUrl,
      error: null,
      providers: [{ displayName: 'GitHub', name: 'github', supportsPassword: false }],
      reachable: true,
      version: '0.20.5'
    })
    oauthLoginConnectionConfig
      .mockResolvedValueOnce({
        ok: false,
        baseUrl: gatewayUrl,
        connected: false,
        error: {
          code: 'native_login_timeout',
          message: 'Loopback callback timed out',
          canRetryEmbedded: true
        }
      })
      .mockResolvedValueOnce({ ok: true, baseUrl: gatewayUrl, connected: false })

    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)
    fireEvent.click(await screen.findByRole('button', { name: /sign in with github/i }))

    await waitFor(() => expect(oauthLoginConnectionConfig).toHaveBeenCalledTimes(1))
    expect(oauthLoginConnectionConfig).toHaveBeenNthCalledWith(1, gatewayUrl)

    const notification = $notifications.get().find(item => item.action?.label === 'Try embedded sign-in')

    expect(notification?.message).toBe('Loopback callback timed out')
    act(() => notification?.action?.onClick())

    await waitFor(() => expect(oauthLoginConnectionConfig).toHaveBeenCalledTimes(2))
    expect(oauthLoginConnectionConfig).toHaveBeenNthCalledWith(2, gatewayUrl, { forceEmbedded: true })
    expect(saveConnectionConfig).toHaveBeenCalledTimes(1)
  })
})
