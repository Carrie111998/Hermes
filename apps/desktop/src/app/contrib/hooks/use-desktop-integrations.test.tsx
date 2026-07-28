import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import {
  $sessions,
  $sessionsLoading,
  getRememberedRoute,
  getRememberedSessionId,
  setRememberedRoute,
  setRememberedSessionId
} from '@/store/session'

import { sessionRoute } from '../../routes'

import { useDesktopIntegrations } from './use-desktop-integrations'

vi.mock('@/store/session-sync', () => ({ onSessionsChanged: vi.fn(() => () => undefined) }))
vi.mock('@/store/updates', () => ({
  openUpdatesWindow: vi.fn(),
  startUpdatePoller: vi.fn(),
  stopUpdatePoller: vi.fn()
}))
vi.mock('@/store/windows', () => ({ isSecondaryWindow: vi.fn(() => false) }))

const BUSINESS_ID = '20260726_161147_719ad3'
type Navigate = (to: string, options?: { replace?: boolean }) => void

function installDesktopBridge(): void {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      onClosePreviewRequested: vi.fn(() => () => undefined),
      onDeepLink: vi.fn(() => () => undefined),
      onFocusSession: vi.fn(() => () => undefined),
      onNotificationAction: vi.fn(() => () => undefined),
      onOpenUpdatesRequested: vi.fn(() => () => undefined),
      setPreviewShortcutActive: vi.fn(),
      signalDeepLinkReady: vi.fn()
    }
  })
}

function renderIntegrations(navigate: Navigate) {
  return renderHook(() =>
    useDesktopIntegrations({
      chatOpen: false,
      hasPreview: false,
      locationPathname: '/',
      navigate,
      refreshSessions: vi.fn(),
      resumeExhaustedSessionId: null,
      routedSessionId: null,
      runtimeIdByStoredSessionId: { current: new Map() }
    })
  )
}

describe('remembered startup route', () => {
  beforeEach(() => {
    localStorage.clear()
    installDesktopBridge()
    $activeGatewayProfile.set('theo')
    $sessions.set([])
    $sessionsLoading.set(false)
  })

  afterEach(() => {
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('reads the remembered route before persisting the initial new-chat route', async () => {
    setRememberedRoute('/skills')
    setRememberedSessionId(BUSINESS_ID, 'theo')
    const navigate = vi.fn<Navigate>()

    renderIntegrations(navigate)

    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/skills', { replace: true }))
    expect(navigate).not.toHaveBeenCalledWith(sessionRoute(BUSINESS_ID), { replace: true })
    expect(getRememberedRoute()).toBe('/skills')
  })

  it('drops an archived remembered session that is absent from the loaded session list', async () => {
    setRememberedRoute('/')
    setRememberedSessionId(BUSINESS_ID, 'theo')
    const navigate = vi.fn<Navigate>()

    renderIntegrations(navigate)

    await waitFor(() => expect(getRememberedSessionId('theo')).toBeNull())
    expect(navigate).not.toHaveBeenCalledWith(sessionRoute(BUSINESS_ID), { replace: true })
  })
})
