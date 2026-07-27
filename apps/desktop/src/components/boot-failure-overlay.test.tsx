import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { $desktopOnboarding } from '@/store/onboarding'

import { BootFailureOverlay } from './boot-failure-overlay'

// Remote-backend users hit a hard boot failure that isn't OAuth reauth (token
// auth, wrong URL, unreachable host). The recovery screen must let them fix the
// remote connection in place — the "Connection settings" action swaps the card
// to an in-line connect form — instead of stranding them (the old bug forced a
// hand-edit of connection.json).

function failBoot() {
  $desktopBoot.set({
    error: 'Could not connect to Hermes gateway',
    fakeMode: false,
    message: 'boot failed',
    phase: 'renderer.error',
    progress: 40,
    running: false,
    timestamp: Date.now(),
    visible: true
  })
}

function stubDesktop(config: Record<string, unknown>) {
  const original = window.hermesDesktop
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { getRecentLogs: async () => ({ lines: [] }), getConnectionConfig: async () => config }
  })

  return () => Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
}

const remoteToken = {
  envOverride: false,
  mode: 'remote',
  profile: null,
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: true,
  remoteUrl: 'http://100.116.104.53:9191',
  cloudOrg: ''
}

beforeEach(() => {
  $desktopOnboarding.set({
    configured: true,
    flow: { status: 'idle' },
    mode: 'oauth',
    providers: null,
    reason: null,
    requested: false,
    firstRunSkipped: false,
    manual: false,
    localEndpoint: false
  })
  failBoot()
})

afterEach(cleanup)

describe('BootFailureOverlay', () => {
  it('swaps to the in-place gateway settings view (no route nav) and back', async () => {
    render(<BootFailureOverlay />)

    fireEvent.click(screen.getByRole('button', { name: /gateway settings/i }))
    // Recovery actions give way to the embedded panel (behind a Back control).
    expect(await screen.findByRole('button', { name: /back/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /back/i }))
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /back/i })).toBeNull()
  })

  it('drops local-only Repair and Use-local-gateway on a local failure', () => {
    render(<BootFailureOverlay />)
    // No connection config stub → treated as a local failure.
    expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /repair/i })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /use local gateway/i })).toBeNull()
  })

  it('leads with Gateway settings and drops Repair for a remote (token) failure', async () => {
    const restore = stubDesktop(remoteToken)

    try {
      render(<BootFailureOverlay />)
      await waitFor(() => expect(screen.queryByRole('button', { name: /repair/i })).toBeNull())
      expect(screen.getByRole('button', { name: /gateway settings/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /use local gateway/i })).toBeTruthy()
    } finally {
      restore()
    }
  })

  it('uses the effective transport for remote reauth while keeping the public identity in UI state', async () => {
    const transport = {
      remoteUrl: 'https://gateway.example.com',
      remotePublicUrl: 'https://gateway.example.com',
      remoteEffectiveUrl: 'http://127.0.0.1:19119',
      remoteTransportMode: 'local_mtls_proxy'
    }
    const desktop = {
      getRecentLogs: vi.fn(async () => ({ lines: [] })),
      getConnectionConfig: vi.fn(async () => ({
        ...remoteToken,
        mode: 'remote',
        remoteAuthMode: 'oauth',
        remoteOauthConnected: false,
        ...transport
      })),
      probeConnectionConfig: vi.fn(async () => ({
        baseUrl: transport.remoteEffectiveUrl,
        effectiveUrl: transport.remoteEffectiveUrl,
        publicUrl: transport.remotePublicUrl,
        reachable: true,
        authMode: 'oauth',
        providers: [{ name: 'nous', displayName: 'Nous Research', supportsPassword: false }],
        transportMode: transport.remoteTransportMode,
        version: null,
        error: null
      })),
      oauthLogoutConnectionConfig: vi.fn(async () => ({ ok: true, connected: false })),
      oauthLoginConnectionConfig: vi.fn(async () => ({
        ok: true,
        baseUrl: transport.remoteEffectiveUrl,
        effectiveUrl: transport.remoteEffectiveUrl,
        publicUrl: transport.remotePublicUrl,
        transportMode: transport.remoteTransportMode,
        connected: false
      }))
    }
    const original = window.hermesDesktop

    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: desktop })

    try {
      render(<BootFailureOverlay />)

      const signIn = await screen.findByRole('button', { name: /sign out (?:&|and) sign in/i })
      await waitFor(() => expect(desktop.probeConnectionConfig).toHaveBeenCalledWith(transport))

      expect(screen.getByText(/sign in with nous research/i)).toBeTruthy()

      fireEvent.click(signIn)
      await waitFor(() => expect(desktop.oauthLogoutConnectionConfig).toHaveBeenCalledWith(transport))
      expect(desktop.oauthLoginConnectionConfig).toHaveBeenCalledWith(transport)
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })
})
