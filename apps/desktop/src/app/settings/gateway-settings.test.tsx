import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionConfig, DesktopConnectionProbeResult } from '@/global'
import type { ProfileInfo } from '@/types/hermes'

const getConnectionConfig = vi.fn()
const saveConnectionConfig = vi.fn()
const profiles = atom<ProfileInfo[]>([])

vi.mock('@/store/profile', () => ({
  $profiles: profiles,
  refreshActiveProfile: vi.fn()
}))

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
  profiles.set([
    {
      has_env: false,
      is_default: true,
      model: null,
      name: 'default',
      path: '/tmp/hermes',
      provider: null,
      skill_count: 0
    },
    {
      has_env: false,
      is_default: false,
      model: null,
      name: 'work',
      path: '/tmp/hermes/profiles/work',
      provider: null,
      skill_count: 0
    }
  ])
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
})

describe('GatewaySettings', () => {
  it('labels local mode as default inheritance for a named profile', async () => {
    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)
    expect(await screen.findByText('Local gateway')).toBeTruthy()
    expect(
      screen.getByText('Start a private Hermes backend on localhost. This is the default and works offline.')
    ).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'work' }))

    await waitFor(() => expect(getConnectionConfig).toHaveBeenLastCalledWith('work'))
    expect(await screen.findByText('Use default gateway')).toBeTruthy()
    expect(screen.getByText("Remove this profile's override and use the default connection.")).toBeTruthy()
    expect(
      screen.queryByText('Start a private Hermes backend on localhost. This is the default and works offline.')
    ).toBeNull()
  })

  it('shows and clears an SSH remote-profile mapping for a named Desktop profile', async () => {
    getConnectionConfig.mockImplementation(async profile =>
      profile === 'work'
        ? {
            ...localConnection,
            mode: 'ssh',
            profile: 'work',
            sshHost: 'remote-box',
            sshUser: 'alice',
            sshPort: 22,
            sshKeyPath: '',
            sshRemoteHermesPath: '/opt/hermes/bin/hermes',
            sshRemoteProfile: 'default'
          }
        : localConnection
    )
    saveConnectionConfig.mockReturnValue(new Promise(() => {}))
    const { GatewaySettings } = await import('./gateway-settings')

    render(<GatewaySettings />)
    fireEvent.click(await screen.findByRole('button', { name: 'work' }))

    await waitFor(() => expect(getConnectionConfig).toHaveBeenLastCalledWith('work'))
    expect(await screen.findByText('Remote profile (optional)')).toBeTruthy()

    const input = screen.getByPlaceholderText('work')

    expect((input as HTMLInputElement).value).toBe('default')
    fireEvent.change(input, { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save for next restart' }))

    await waitFor(() =>
      expect(saveConnectionConfig).toHaveBeenCalledWith(
        expect.objectContaining({
          profile: 'work',
          sshRemoteProfile: ''
        })
      )
    )
  })
})

// The dedicated boot-failure reauth button (65712bf78) clears the OAuth
// partition before opening the login window so a stale identity-provider
// cookie can't bounce a fresh sign-in back into the same broken session. The
// routine Settings -> Gateway "Sign in" button is the OTHER door into the
// exact same oauth-login IPC call and was left doing a bare login with no
// logout first — this proves it now clears the partition first too.

const OAUTH_URL = 'https://gateway.example.com'

function oauthBaseConfig(patch: Partial<DesktopConnectionConfig> = {}): DesktopConnectionConfig {
  return {
    envOverride: false,
    mode: 'remote',
    profile: null,
    remoteAuthMode: 'oauth',
    remoteOauthConnected: false,
    remoteTokenPreview: null,
    remoteTokenSet: true, // lets authResolved settle without waiting on the debounced probe
    remoteUrl: OAUTH_URL,
    cloudOrg: '',
    ...patch
  }
}

function oauthProbeResult(): DesktopConnectionProbeResult {
  return {
    baseUrl: OAUTH_URL,
    reachable: true,
    authMode: 'oauth',
    providers: [{ name: 'nous', displayName: 'Nous', supportsPassword: false }],
    version: null,
    error: null
  }
}

function stubOauthDesktop(config: DesktopConnectionConfig) {
  const calls: string[] = []

  const oauthLogoutConnectionConfig = vi.fn(async () => {
    calls.push('logout')

    return { ok: true, connected: false }
  })

  const oauthLoginConnectionConfig = vi.fn(async () => {
    calls.push('login')

    return { ok: true, baseUrl: OAUTH_URL, connected: true }
  })

  const original = window.hermesDesktop
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      getConnectionConfig: async () => config,
      saveConnectionConfig: async (payload: unknown) => ({ ...config, ...(payload as object) }),
      probeConnectionConfig: async () => oauthProbeResult(),
      oauthLoginConnectionConfig,
      oauthLogoutConnectionConfig
    }
  })

  return {
    calls,
    oauthLoginConnectionConfig,
    oauthLogoutConnectionConfig,
    restore: () => Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
  }
}

describe('GatewaySettings sign-in', () => {
  it('clears the OAuth partition before opening the login window', async () => {
    const desktop = stubOauthDesktop(oauthBaseConfig())

    try {
      const { GatewaySettings } = await import('./gateway-settings')

      render(<GatewaySettings embedded />)

      // The auth-mode probe is debounced 500ms; the button only renders once
      // it resolves. Its label is "Sign in with <provider>" (the probe's
      // single non-password provider, "Nous") — an exact match, since "Sign
      // in to Hermes Cloud" (the cloud-mode panel, always rendered alongside
      // the mode picker) also contains the substring "sign in".
      const signInButton = await screen.findByRole('button', { name: 'Sign in with Nous' }, { timeout: 3000 })
      fireEvent.click(signInButton)

      await waitFor(() => expect(desktop.oauthLoginConnectionConfig).toHaveBeenCalled(), { timeout: 2000 })

      expect(desktop.oauthLogoutConnectionConfig).toHaveBeenCalledWith(OAUTH_URL)
      expect(desktop.oauthLoginConnectionConfig).toHaveBeenCalledWith(OAUTH_URL)
      // Logout must complete before the login window opens, not just both fire.
      expect(desktop.calls).toEqual(['logout', 'login'])
    } finally {
      desktop.restore()
    }
  })
})
