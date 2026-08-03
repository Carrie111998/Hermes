import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $desktopBoot } from '@/store/boot'
import { $gatewaySwitching } from '@/store/gateway-switch'
import { $desktopOnboarding } from '@/store/onboarding'
import { setGatewayState } from '@/store/session'

import { BootFailureOverlay } from './boot-failure-overlay'
import { GatewayConnectingOverlay } from './gateway-connecting-overlay'

// Repro for the "remote gateway → stuck on CONNECTING, no way to settings"
// report. The connecting overlay (full-screen, pointer-events on) used
// to be shown whenever `gatewayState !== 'open' && !boot.error`. The ONLY escape
// hatch — BootFailureOverlay, which has "Use local gateway" / "Sign in" /
// "Retry" — only renders when `boot.error` is set.
//
// useGatewayBoot only calls failDesktopBoot() (which sets boot.error) when the
// INITIAL boot() throws. After the first successful connect (bootCompleted),
// any later socket drop goes through scheduleReconnect(), which loops FOREVER
// against the dead remote. So gatewayState sits at 'closed'/'error' with
// boot.error null. The fix keeps the initial-boot overlay out of post-boot
// reconnects, leaving chat/settings usable while the reconnect loop runs.

function resetStores() {
  setGatewayState('idle')
  $gatewaySwitching.set(false)
  $desktopBoot.set({
    error: null,
    fakeMode: false,
    message: 'ready',
    phase: 'renderer.ready',
    progress: 100,
    running: false,
    timestamp: Date.now(),
    visible: false
  })
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
}

beforeEach(resetStores)
afterEach(cleanup)

// The connecting overlay renders "CONN" + a scrambled tail inside one
// uppercase span; match that node specifically so the recovery overlay's
// "Lost connection…" copy doesn't read as a false positive.
const isConnectingShown = () =>
  screen.queryAllByText((_, el) => /^CONN[/\\|\-_=+<>~:*A-Z]*$/.test(el?.textContent?.trim() ?? '')).length > 0

const isRecoveryShown = () =>
  Boolean(screen.queryByText(/use local gateway/i) || screen.queryByText(/retry/i) || screen.queryByText(/sign in/i))

describe('connecting overlay vs recovery surface', () => {
  it('hard initial-boot failure surfaces the recovery overlay (the working path)', async () => {
    // failDesktopBoot() ran: error set, gateway never opened.
    $desktopBoot.set({
      ...$desktopBoot.get(),
      error: 'Hermes backend did not become ready',
      running: false,
      visible: true
    })
    setGatewayState('error')

    await act(async () => {
      render(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })

    expect(isRecoveryShown()).toBe(true)
    // Connecting overlay bows out when boot.error is set.
    expect(isConnectingShown()).toBe(false)
  })

  it('post-boot socket drops do not re-cover the app with the initial CONNECTING overlay', async () => {
    // 1. Initial boot succeeded: gateway opened, boot completed (no error).
    setGatewayState('open')

    let rerender!: (ui: React.ReactElement) => void
    await act(async () => {
      const result = render(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )

      rerender = result.rerender
    })

    expect(isConnectingShown()).toBe(false)

    // 2. The remote VPS socket drops (sleep/wake, remote restart, network).
    //    bootCompleted is true, so useGatewayBoot routes this through
    //    scheduleReconnect() — boot.error stays NULL.
    await act(async () => {
      setGatewayState('closed')
      rerender!(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })

    // The initial-boot connecting overlay stays out of the way, so settings and
    // the composer remain reachable during the reconnect loop.
    expect(isConnectingShown()).toBe(false)
    expect(isRecoveryShown()).toBe(false)

    // 3. Reconnect loops against the dead remote: gatewayState bounces closed
    //    → error → closed. Until the escalation path sets boot.error, the app
    //    remains usable instead of modal-blocked.
    await act(async () => {
      setGatewayState('error')
      rerender!(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })
    expect($desktopBoot.get().error).toBeNull()
    expect(isConnectingShown()).toBe(false)
    expect(isRecoveryShown()).toBe(false)
  })

  it('soft gateway switch keeps the shell — no fullscreen CONNECTING', async () => {
    setGatewayState('open')

    const { rerender } = render(
      <>
        <GatewayConnectingOverlay />
        <BootFailureOverlay />
      </>
    )

    await act(async () => {
      $gatewaySwitching.set(true)
      $desktopBoot.set({
        ...$desktopBoot.get(),
        running: true,
        visible: true,
        progress: 4,
        error: null
      })
      setGatewayState('closed')
      rerender(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })

    expect(isConnectingShown()).toBe(false)
    expect(isRecoveryShown()).toBe(false)
  })

  it('managed Eva enrollment leaves Settings reachable before a gateway exists', () => {
    setGatewayState('idle')
    $desktopBoot.set({
      ...$desktopBoot.get(),
      phase: 'renderer.enrollment',
      progress: 100,
      running: false,
      visible: false
    })

    render(
      <>
        <GatewayConnectingOverlay />
        <BootFailureOverlay />
      </>
    )

    expect(isConnectingShown()).toBe(false)
    expect(isRecoveryShown()).toBe(false)
  })

  it('managed Eva main-process sign-in state also leaves Settings visible', () => {
    setGatewayState('idle')
    $desktopBoot.set({
      ...$desktopBoot.get(),
      phase: 'eva.sign-in-required',
      message: 'Sign in to evaOS Agent from Settings → Gateway.',
      progress: 8,
      running: false,
      visible: true
    })

    render(
      <>
        <GatewayConnectingOverlay />
        <BootFailureOverlay />
      </>
    )

    expect(isConnectingShown()).toBe(false)
    expect(isRecoveryShown()).toBe(false)
  })

  it('managed Eva hard failures expose only managed retry and sign-in recovery', () => {
    const originalDesktop = window.hermesDesktop
    window.hermesDesktop = {
      eva: {
        refresh: async () => ({
          managed: true,
          productName: 'evaOS Agent',
          signedOut: false,
          customerId: 'jackie-david',
          email: null,
          desktopSessionExpiresAt: null,
          desktopSessionActive: false,
          runtimeSessionExpiresAt: null,
          runtimeSessionActive: false,
          agentId: null,
          updateChannel: 'managed-beta'
        }),
        signIn: async () => ({
          managed: true,
          productName: 'evaOS Agent',
          signedOut: false,
          customerId: 'jackie-david',
          email: null,
          desktopSessionExpiresAt: null,
          desktopSessionActive: false,
          runtimeSessionExpiresAt: null,
          runtimeSessionActive: false,
          agentId: null,
          updateChannel: 'managed-beta'
        })
      }
    } as unknown as Window['hermesDesktop']
    $desktopBoot.set({
      ...$desktopBoot.get(),
      error: 'Managed backend unavailable',
      running: false,
      visible: true
    })

    try {
      render(<BootFailureOverlay />)
      expect(screen.getByRole('button', { name: /retry/i })).toBeTruthy()
      expect(screen.getByRole('button', { name: /sign in/i })).toBeTruthy()
      expect(screen.queryByRole('button', { name: /use local gateway/i })).toBeNull()
      expect(screen.queryByRole('button', { name: /repair/i })).toBeNull()
      expect(screen.queryByRole('button', { name: /open logs/i })).toBeNull()
    } finally {
      cleanup()
      window.hermesDesktop = originalDesktop
    }
  })

  it('FIX: once the prolonged reconnect raises a recoverable boot error, the recovery overlay takes over', async () => {
    // Mirrors what useGatewayBoot.scheduleReconnect() now does after ~45s of
    // failed post-boot reconnects: it calls failDesktopBoot(), flipping the UI
    // from the dead-end CONNECTING overlay to the recovery surface.
    setGatewayState('error')
    $desktopBoot.set({
      ...$desktopBoot.get(),
      error: 'Lost connection to the Hermes gateway and could not reconnect.',
      running: false,
      visible: true
    })

    await act(async () => {
      render(
        <>
          <GatewayConnectingOverlay />
          <BootFailureOverlay />
        </>
      )
    })

    // Escape hatch is now reachable; the connecting overlay bows out.
    expect(isRecoveryShown()).toBe(true)
    expect(screen.getByRole('button', { name: /gateway settings/i })).toBeTruthy()
    expect(isConnectingShown()).toBe(false)
  })
})
