// @vitest-environment jsdom
// @vitest-environment-options {"url":"http://localhost/?win=secondary&profile=life#/session-123"}

import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'

import { useGatewayBoot } from './use-gateway-boot'

function Harness() {
  useGatewayBoot({
    beforeConnectionSwitch: () => undefined,
    handleGatewayEvent: () => undefined,
    onConnectionReady: () => undefined,
    onGatewayReady: () => undefined,
    refreshHermesConfig: async () => undefined,
    refreshSessions: async () => undefined
  })

  return null
}

describe('useGatewayBoot secondary-window profile routing', () => {
  afterEach(() => {
    cleanup()
    $activeGatewayProfile.set('default')
    delete (window as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('scopes the session from the URL while main selects the bound backend', () => {
    // Keep the connection pending: this covers the first boot hop from the real
    // window URL parser through useGatewayBoot to the Electron bridge.
    const getConnection = vi.fn(() => new Promise<never>(() => undefined))

    ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
      getConnection,
      getBootProgress: vi.fn(() => new Promise<never>(() => undefined)),
      onBackendExit: vi.fn(() => () => undefined),
      onBootProgress: vi.fn(() => () => undefined)
    }

    render(<Harness />)

    expect($activeGatewayProfile.get()).toBe('life')
    expect(getConnection).toHaveBeenCalledOnce()
    expect(getConnection).toHaveBeenCalledWith()
  })
})
