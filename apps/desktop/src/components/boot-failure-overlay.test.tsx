import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { $notifications, clearNotifications } from '@/store/notifications'
import { $desktopOnboarding } from '@/store/onboarding'

import { BootFailureOverlay, completeManagedSignIn } from './boot-failure-overlay'

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
  clearNotifications()
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

afterEach(() => {
  cleanup()
  clearNotifications()
})

describe('BootFailureOverlay', () => {
  it('reloads only after managed sign-in succeeds', async () => {
    const reload = vi.fn()

    await expect(completeManagedSignIn(() => Promise.reject(new Error('sign-in failed')), reload)).rejects.toThrow(
      'sign-in failed'
    )
    expect(reload).not.toHaveBeenCalled()

    await completeManagedSignIn(() => Promise.resolve(), reload)
    expect(reload).toHaveBeenCalledOnce()
  })

  it('localizes managed assignment recovery and keeps failed sign-in retryable', async () => {
    const original = window.hermesDesktop
    const signIn = vi.fn().mockRejectedValue(new Error('managed broker unavailable'))

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { eva: { signIn } },
      writable: true
    })

    try {
      render(<BootFailureOverlay />)

      expect(
        screen.getByText(
          'Your business assignment is selected by Electric Sheep. Sign in again if access was changed or revoked.'
        )
      ).toBeTruthy()

      const button = screen.getByRole('button', { name: 'Sign in to remote gateway' })
      fireEvent.click(button)

      await waitFor(() => expect(signIn).toHaveBeenCalledOnce())
      await waitFor(() =>
        expect($notifications.get()).toEqual([
          expect.objectContaining({
            kind: 'error',
            title: 'Could not sign in to managed access. Try again.'
          })
        ])
      )
      expect((button as HTMLButtonElement).disabled).toBe(false)
    } finally {
      Object.defineProperty(window, 'hermesDesktop', {
        configurable: true,
        value: original,
        writable: true
      })
    }
  })

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
})
