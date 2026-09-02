import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const cloudDiscover = vi.fn()
const cloudStatus = vi.fn()
const getConnectionConfig = vi.fn()
const saveConnectionConfig = vi.fn()

// This test owns the machine-level GatewaySettings contract. The managed SSH
// update section mounted below the registry has its own focused coverage
// (store/managed-updates.test.ts); keep its store subscriptions out of this
// single-purpose test.
vi.mock('./managed-updates-section', () => ({ ManagedUpdatesSection: () => null }))

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
  cloudStatus.mockResolvedValue({ portalBaseUrl: 'https://portal.nousresearch.com', signedIn: true })
  cloudDiscover.mockResolvedValue({ agents: [], org: null })
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      cloud: {
        discover: cloudDiscover,
        status: cloudStatus
      },
      getConnectionConfig,
      saveConnectionConfig
    }
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

  it('hides an unknown cloud agent gateway status', async () => {
    cloudDiscover.mockResolvedValue({
      agents: [
        {
          dashboardGatewayState: 'unknown',
          dashboardUrl: 'https://agent.example.com',
          id: 'agent-1',
          name: 'Cloud Agent',
          status: 'active'
        }
      ],
      org: null
    })
    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)
    fireEvent.click(await screen.findByRole('button', { name: /Hermes Cloud/ }))

    expect(await screen.findByText('Cloud Agent')).toBeTruthy()
    expect(screen.queryByText('Status: unknown')).toBeNull()
  })

  it('hides an empty cloud agent gateway status', async () => {
    cloudDiscover.mockResolvedValue({
      agents: [
        {
          dashboardGatewayState: '',
          dashboardUrl: 'https://agent.example.com',
          id: 'agent-1',
          name: 'Cloud Agent',
          status: 'active'
        }
      ],
      org: null
    })
    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)
    fireEvent.click(await screen.findByRole('button', { name: /Hermes Cloud/ }))

    expect(await screen.findByText('Cloud Agent')).toBeTruthy()
    expect(screen.queryByText(/^Status:/)).toBeNull()
  })

  it('shows a known cloud agent gateway status', async () => {
    cloudDiscover.mockResolvedValue({
      agents: [
        {
          dashboardGatewayState: 'active',
          dashboardUrl: 'https://agent.example.com',
          id: 'agent-1',
          name: 'Cloud Agent',
          status: 'active'
        }
      ],
      org: null
    })
    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)
    fireEvent.click(await screen.findByRole('button', { name: /Hermes Cloud/ }))

    expect(await screen.findByText('Status: active')).toBeTruthy()
  })
})
