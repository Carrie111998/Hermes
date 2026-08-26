import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'
import { $layoutTree, activateTreePane, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import { $pendingConnectionId } from '@/store/connections'
import {
  $profileSwitchRestoreToken,
  _resetProfileSwitchBehaviorForTests,
  bindProfileSwitchRestore,
  clearProfileSwitchRestore,
  requestProfileSwitchRestore,
  setProfileSwitchBehavior
} from '@/store/profile-switch-behavior'
import { $freshSessionRequest, requestFreshSession } from '@/store/profile'
import { _resetLegacyDiscardForTests, setRememberedSessionId } from '@/store/session'
import type { SessionOwnerScope } from '@/store/session-request-router'

import { deferred } from '../../../test/deferred'

import { useDesktopIntegrations } from './use-desktop-integrations'

const mocks = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof import('nanostores')

  return {
    activeConnectionId: null as null | string,
    activationEpoch: 0,
    browser: false,
    focusOpenSession: vi.fn<() => 'main' | 'tile' | null>(() => null),
    focusedStoredSessionId: atom<null | string>(null),
    hud: false,
    knownOwner: vi.fn<(sessionId: null | string | undefined) => SessionOwnerScope>(() => undefined),
    markSelectionRestore: vi.fn(),
    openSession: vi.fn(),
    requestSessionResume: vi.fn(),
    secondary: false
  }
})

vi.mock('@/app/open-session', () => ({ openSession: mocks.openSession }))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  activeGatewayConnectionId: () => mocks.activeConnectionId,
  gatewayActivationEpoch: () => mocks.activationEpoch
}))

vi.mock('@/store/session', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestSessionResume: mocks.requestSessionResume
}))

vi.mock('@/store/session-states', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  $focusedStoredSessionId: mocks.focusedStoredSessionId,
  focusOpenSession: mocks.focusOpenSession,
  knownOwnerForSession: mocks.knownOwner,
  markSelectionRestore: mocks.markSelectionRestore
}))

vi.mock('@/store/windows', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  isBrowserWindow: () => mocks.browser,
  isHudWindow: () => mocks.hud,
  isSecondaryWindow: () => mocks.secondary
}))

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

interface HookProps {
  activeProfile: string
  descriptorConnectionId: null | string
  locationPathname: string
  profileReady: boolean
  refreshSessions: (shouldPublish?: () => boolean) => Promise<boolean>
}

describe('live profile-switch continuity', () => {
  beforeEach(() => {
    localStorage.clear()
    _resetLegacyDiscardForTests()
    _resetProfileSwitchBehaviorForTests()
    $pendingConnectionId.set(null)
    mocks.activeConnectionId = 'source-a'
    mocks.activationEpoch = 7
    mocks.browser = false
    mocks.hud = false
    mocks.secondary = false
    mocks.focusedStoredSessionId.set(null)
    $layoutTree.set(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'main-zone' }),
        group(['preview', 'terminal'], { active: 'preview', id: 'native-zone' })
      ])
    )
    noteActiveTreeGroup(null)
    mocks.focusOpenSession.mockReset()
    mocks.focusOpenSession.mockReturnValue(null)
    mocks.knownOwner.mockReset()
    mocks.knownOwner.mockReturnValue(undefined)
    mocks.markSelectionRestore.mockReset()
    mocks.openSession.mockReset()
    mocks.requestSessionResume.mockReset()

    desktopWindow.hermesDesktop = {
      onClosePreviewRequested: vi.fn(),
      onDeepLink: vi.fn(),
      onFocusSession: vi.fn(),
      onNotificationAction: vi.fn(),
      onNotificationActivate: vi.fn(),
      onOpenFolderRequested: vi.fn(),
      onOpenUpdatesRequested: vi.fn(),
      setPreviewShortcutActive: vi.fn(),
      signalDeepLinkReady: vi.fn()
    } as unknown as Window['hermesDesktop']
  })

  afterEach(() => {
    desktopWindow.hermesDesktop = initialHermesDesktop
  })

  function renderContinuity(initial: Partial<HookProps> = {}) {
    const navigate = vi.fn()
    const defaults: HookProps = {
      activeProfile: 'alpha',
      descriptorConnectionId: 'source-a',
      locationPathname: '/settings',
      profileReady: true,
      refreshSessions: vi.fn(async () => true)
    }

    const result = renderHook(
      (overrides: Partial<HookProps>) => {
        const props = { ...defaults, ...overrides }

        useDesktopIntegrations({
          activeProfile: props.activeProfile,
          chatOpen: false,
          descriptorConnectionId: props.descriptorConnectionId,
          descriptorProfile: props.activeProfile,
          hasPreview: false,
          locationPathname: props.locationPathname,
          navigate,
          profileReady: props.profileReady,
          refreshSessions: props.refreshSessions,
          resumeExhaustedSessionId: null,
          routedSessionId: null,
          runtimeIdByStoredSessionId: { current: new Map() },
          sessions: []
        })
      },
      { initialProps: initial }
    )

    return { ...result, navigate }
  }

  async function publishRestore(
    profile = 'alpha',
    requestedConnectionId: null | string = 'source-a',
    liveGatewayConnectionId: null | string = requestedConnectionId,
    descriptorConnectionId: null | string = requestedConnectionId
  ) {
    await act(async () => {
      setProfileSwitchBehavior('restore_last_session')
      const freshSessionRequestSequence = $freshSessionRequest.get() + 1
      const token = requestProfileSwitchRestore(profile, requestedConnectionId, freshSessionRequestSequence)
      requestFreshSession(token.generation)
      bindProfileSwitchRestore(token.generation, {
        activationEpoch: mocks.activationEpoch,
        descriptorConnectionId,
        descriptorProfile: profile,
        liveGatewayConnectionId,
        profile
      })
      await Promise.resolve()
    })
  }

  it('lets pre-activation user navigation consume only its restore generation', async () => {
    const refreshSessions = vi.fn(async () => true)
    const result = renderContinuity({ locationPathname: '/session/source', refreshSessions })
    let oldGeneration = 0

    await act(async () => {
      setProfileSwitchBehavior('restore_last_session')
      const token = requestProfileSwitchRestore('alpha', 'source-a', $freshSessionRequest.get() + 1)
      oldGeneration = token.generation
      requestFreshSession(token.generation)
    })

    result.rerender({ locationPathname: '/skills', refreshSessions })

    expect($profileSwitchRestoreToken.get()).toBeNull()
    expect(refreshSessions).not.toHaveBeenCalled()
    expect(mocks.focusOpenSession).not.toHaveBeenCalled()
    expect(mocks.requestSessionResume).not.toHaveBeenCalled()
    expect(mocks.openSession).not.toHaveBeenCalled()

    let newerGeneration = 0

    await act(async () => {
      const token = requestProfileSwitchRestore('beta', 'source-a', $freshSessionRequest.get() + 1)
      newerGeneration = token.generation
      requestFreshSession(token.generation)
      clearProfileSwitchRestore(oldGeneration)
    })

    expect($profileSwitchRestoreToken.get()?.generation).toBe(newerGeneration)
  })

  it('cancels when the user navigates before the committed refresh returns', async () => {
    const refresh = deferred<boolean>()
    const refreshSessions = vi.fn(() => refresh.promise)
    const result = renderContinuity({ locationPathname: '/', refreshSessions })

    mocks.knownOwner.mockReturnValue({ connectionId: 'source-a', profile: 'alpha' })
    setRememberedSessionId('alpha-main', 'alpha')
    await publishRestore()

    result.rerender({ locationPathname: '/skills', refreshSessions })
    await act(async () => refresh.resolve(true))

    expect($profileSwitchRestoreToken.get()).toBeNull()
    expect(mocks.focusOpenSession).not.toHaveBeenCalled()
    expect(mocks.openSession).not.toHaveBeenCalled()
  })

  it('does not restore over native focus moved to a non-session pane during refresh', async () => {
    const refresh = deferred<boolean>()
    const refreshSessions = vi.fn(() => refresh.promise)
    const result = renderContinuity({ locationPathname: '/', refreshSessions })

    mocks.knownOwner.mockReturnValue({ connectionId: 'source-a', profile: 'alpha' })
    setRememberedSessionId('alpha-main', 'alpha')
    await publishRestore()

    expect(refreshSessions).toHaveBeenCalledOnce()
    expect($profileSwitchRestoreToken.get()?.draftNavigationToken).toBeDefined()

    await act(async () => {
      activateTreePane('native-zone', 'terminal')
      noteActiveTreeGroup('native-zone')
      await Promise.resolve()
    })

    expect($profileSwitchRestoreToken.get()).toBeNull()

    await act(async () => refresh.resolve(true))

    expect(mocks.focusOpenSession).not.toHaveBeenCalled()
    expect(mocks.requestSessionResume).not.toHaveBeenCalled()
    expect(mocks.openSession).not.toHaveBeenCalled()
    expect(result.navigate).not.toHaveBeenCalled()
  })

  it('does not seal a fresh draft when the null group marker masks an active terminal beside workspace', async () => {
    $layoutTree.set(group(['workspace', 'terminal'], { active: 'terminal', id: 'main-zone' }))
    noteActiveTreeGroup(null)
    mocks.knownOwner.mockReturnValue({ connectionId: 'source-a', profile: 'alpha' })
    setRememberedSessionId('alpha-main', 'alpha')
    const refreshSessions = vi.fn(async () => true)

    renderContinuity({ locationPathname: '/', refreshSessions })
    await publishRestore()

    expect($profileSwitchRestoreToken.get()).toBeNull()
    expect(refreshSessions).not.toHaveBeenCalled()
    expect(mocks.focusOpenSession).not.toHaveBeenCalled()
    expect(mocks.requestSessionResume).not.toHaveBeenCalled()
    expect(mocks.openSession).not.toHaveBeenCalled()
  })

  it('accepts workspace focus from the workspace group when the active group marker is null', async () => {
    const refresh = deferred<boolean>()
    const refreshSessions = vi.fn(() => refresh.promise)

    $layoutTree.set(group(['workspace', 'terminal'], { active: 'workspace', id: 'main-zone' }))
    noteActiveTreeGroup(null)
    mocks.knownOwner.mockReturnValue({ connectionId: 'source-a', profile: 'alpha' })
    setRememberedSessionId('alpha-main', 'alpha')

    renderContinuity({ locationPathname: '/', refreshSessions })
    await publishRestore()

    expect(refreshSessions).toHaveBeenCalledOnce()
    expect($profileSwitchRestoreToken.get()?.draftNavigationToken).toBeDefined()

    await act(async () => refresh.resolve(true))

    expect(mocks.requestSessionResume).toHaveBeenCalledWith('alpha-main', {
      connectionId: 'source-a',
      profile: 'alpha'
    })
    expect(mocks.openSession).toHaveBeenCalledOnce()
  })

  it.each([
    ['uncommitted refresh', vi.fn(async () => false)],
    ['failed refresh', vi.fn(async () => Promise.reject(new Error('refresh failed')))]
  ])('falls back to the fresh draft after %s', async (_label, refreshSessions) => {
    mocks.knownOwner.mockReturnValue({ connectionId: 'source-a', profile: 'alpha' })
    setRememberedSessionId('alpha-main', 'alpha')
    renderContinuity({ locationPathname: '/', refreshSessions })

    await publishRestore()

    expect($profileSwitchRestoreToken.get()).toBeNull()
    expect(mocks.openSession).not.toHaveBeenCalled()
  })

  it('does not accept the same profile name from another connection source', async () => {
    const refreshSessions = vi.fn(async () => true)

    mocks.activeConnectionId = 'source-b'
    mocks.knownOwner.mockReturnValue({ connectionId: 'source-a', profile: 'default' })
    setRememberedSessionId('shared-name', 'default')
    renderContinuity({
      activeProfile: 'default',
      descriptorConnectionId: 'source-b',
      locationPathname: '/',
      refreshSessions
    })

    await publishRestore('default', 'source-a')

    expect(refreshSessions).not.toHaveBeenCalled()
    expect(mocks.openSession).not.toHaveBeenCalled()
  })

  it.each(['hud', 'browser', 'secondary'] as const)('never runs live restore effects in a %s window', async kind => {
    mocks[kind] = true
    mocks.knownOwner.mockReturnValue({ connectionId: 'source-a', profile: 'alpha' })
    setRememberedSessionId('alpha-main', 'alpha')
    const refreshSessions = vi.fn(async () => true)

    renderContinuity({ locationPathname: '/', refreshSessions })
    await publishRestore()

    expect($profileSwitchRestoreToken.get()).toBeNull()
    expect(refreshSessions).not.toHaveBeenCalled()
    expect(mocks.openSession).not.toHaveBeenCalled()
  })
})
