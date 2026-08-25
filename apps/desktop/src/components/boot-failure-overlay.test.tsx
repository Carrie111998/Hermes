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

  it('shows the Nous Cloud down recovery when the backend flags isCloudBackendDown', async () => {
    const restore = stubDesktop(remoteToken)
    $desktopBoot.set({
      error: 'Nous Cloud agent ares-3009.agents.nousresearch.com is down (HTTP 503: server-side fault).',
      fakeMode: false,
      isCloudBackendDown: true,
      message: 'boot failed',
      phase: 'renderer.error',
      progress: 40,
      running: false,
      statusCode: 503,
      timestamp: Date.now(),
      visible: true
    })

    try {
      render(<BootFailureOverlay />)
      // Cloud-specific title + actionable recovery instead of the generic
      // remote-failure copy.
      expect(await screen.findByText(/Nous Cloud agent is down/i)).toBeTruthy()
      // Portal and Discord are dedicated action buttons (localized labels
      // can't drift the URLs, which live in code).
      expect(screen.getByRole('button', { name: /check portal status/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /get help on discord/i })).toBeTruthy()
      // Cloud-down is a remote failure: local-only Repair is dropped; the
      // actionable paths are Gateway settings + Use local gateway.
      expect(screen.queryByRole('button', { name: /repair/i })).toBeNull()
      expect(screen.getByRole('button', { name: /gateway settings/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /use local gateway/i })).toBeTruthy()
      // The electron-built error message (portal / local mode / Discord) is
      // still surfaced in the error box.
      expect(screen.getByText(/ares-3009\.agents\.nousresearch\.com/i)).toBeTruthy()
    } finally {
      restore()
    }
  })

  it('scopes the pre-signin oauth logout to THIS gateway URL (no partition-wide wipe)', async () => {
    // Boot failure on an OAuth remote gateway whose session lapsed → the
    // recovery card offers "Sign out & sign in". The pre-login clear MUST be
    // scoped to the gateway's own origin. Before #94856 the argument-less
    // oauthLogoutConnectionConfig() cleared EVERY hostname's cookies in the
    // shared `persist:hermes-remote-oauth` partition — silently signing out
    // every other registered gateway, not just the one being re-authenticated.
    const logout = vi.fn().mockResolvedValue({ ok: true, connected: false })
    const login = vi.fn().mockResolvedValue({ ok: true, baseUrl: 'https://box:8443', connected: false })

    const reauthConfig = {
      cloudOrg: '',
      envOverride: false,
      mode: 'remote',
      remoteAuthMode: 'oauth',
      remoteOauthConnected: false,
      remoteTokenPreview: null,
      remoteTokenSet: false,
      remoteUrl: 'https://box:8443'
    }

    const original = window.hermesDesktop
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnectionConfig: async () => reauthConfig,
        getRecentLogs: async () => ({ lines: [] }),
        oauthLoginConnectionConfig: login,
        oauthLogoutConnectionConfig: logout,
        probeConnectionConfig: async () => ({
          authMode: 'oauth',
          baseUrl: 'https://box:8443',
          error: null,
          providers: [],
          reachable: true,
          version: null
        })
      }
    })

    try {
      render(<BootFailureOverlay />)

      // Reauth surfaces the "Sign out & sign in" recovery action.
      const signIn = await screen.findByRole('button', { name: /sign out & sign in/i })
      fireEvent.click(signIn)

      // The logout must be scoped to the gateway origin, never argument-less.
      await waitFor(() => expect(logout).toHaveBeenCalledWith('https://box:8443'))
      expect(logout).not.toHaveBeenCalledWith(undefined)
      expect(logout).not.toHaveBeenCalledWith()
      // The login window must open for the SAME gateway.
      expect(login).toHaveBeenCalledWith('https://box:8443')
    } finally {
      Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: original })
    }
  })
})
