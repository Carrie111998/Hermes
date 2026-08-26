import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { leaveGatewaySettingsForReconnect } from './gateway-settings'

const getConnectionConfig = vi.fn()
const saveConnectionConfig = vi.fn()

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
  getConnectionConfig.mockResolvedValue(localConnection)
  saveConnectionConfig.mockResolvedValue(localConnection)
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { getConnectionConfig, saveConnectionConfig }
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  window.history.replaceState(window.history.state, '', '#/')
})

describe('GatewaySettings', () => {
  it('leaves the gateway Settings hash before applying a reconnect', () => {
    const navigate = vi.fn()
    window.history.replaceState(window.history.state, '', '#/settings?tab=gateway')

    leaveGatewaySettingsForReconnect(navigate)

    expect(window.location.hash).toBe('#/')
    expect(navigate).toHaveBeenCalledWith('/', { replace: true })
  })

  it('loads the machine-level connection config (no profile scoping)', async () => {
    const { GatewaySettings } = await import('./gateway-settings')

    render(
      <MemoryRouter>
        <GatewaySettings />
      </MemoryRouter>
    )
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
})
