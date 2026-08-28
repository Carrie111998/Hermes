import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { requestMcpInstallFromDeepLink } from '@/store/mcp-deeplink-install'
import { gatewayActivationEpoch } from '@/store/gateway'
import {
  $appliedFreshDraftProvenance,
  $profileConversationRestore,
  _resetProfileConversationRestoreForTests,
  applyFreshDraftProvenance,
  beginProfileConversationRestore,
  commitProfileConversationRestore
} from '@/store/profile-conversation-restore'
import { _resetLegacyDiscardForTests, getRememberedConversation, setRememberedConversation } from '@/store/session'
import type * as WindowsStore from '@/store/windows'
import type { SessionInfo } from '@/types/hermes'
import type { HermesConnection } from '@/global'

import { makeSessionInfo } from '../../../test/session-info'

import { type ConversationRestoreScope, useDesktopIntegrations } from './use-desktop-integrations'

// Mutable HUD-window flag so the restore tests can flip the window kind the
// hook believes it runs in. Default false keeps the pre-existing restore
// coverage exercising the real main-window path.
const { hudWindowMock, publishRestoreMock, restoreLookupMock } = vi.hoisted(() => ({
  hudWindowMock: vi.fn(() => false),
  publishRestoreMock: vi.fn(),
  restoreLookupMock: vi.fn()
}))

vi.mock('../../session/hooks/use-session-actions/utils', () => ({
  publishResolvedSessionForRestore: publishRestoreMock,
  resolveStoredSessionForRestore: restoreLookupMock
}))

vi.mock('@/store/mcp-deeplink-install', () => ({
  requestMcpInstallFromDeepLink: vi.fn()
}))

vi.mock('@/store/windows', async importOriginal => {
  const actual = await importOriginal<typeof WindowsStore>()

  return {
    ...actual,
    isHudWindow: () => hudWindowMock()
  }
})

// Pure-jsdom localStorage (no nanostores persistence module needed — the
// production functions write directly to window.localStorage through the
// persistString/storedString helpers in @/lib/storage, which in jsdom resolves
// to the real localStorage global).
// We import the hook and drive it with explicit rx-stores/props to exercise the
// profile-ready gate, ownership validation, and legacy-key discard.

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

const session = (over: Partial<SessionInfo> = {}): SessionInfo => makeSessionInfo({ id: 'live', ...over })

describe('useDesktopIntegrations', () => {
  let navigate: ReturnType<typeof vi.fn<(...args: unknown[]) => void>>

  beforeEach(() => {
    window.localStorage.clear()
    _resetLegacyDiscardForTests()
    _resetProfileConversationRestoreForTests()
    publishRestoreMock.mockReset()
    restoreLookupMock.mockReset()
    restoreLookupMock.mockResolvedValue({ status: 'found', session: session() })
    vi.mocked(requestMcpInstallFromDeepLink).mockClear()
    navigate = vi.fn()
    // Every test starts as a main window; only the HUD describe flips this.
    hudWindowMock.mockReturnValue(false)

    // Stub the desktop bridge so the hook's useEffect callbacks don't try to
    // reach real Electron IPC. The established desktop-test pattern assigns a
    // plain object to window.hermesDesktop rather than using vi.spyOn.
    desktopWindow.hermesDesktop = {
      setPreviewShortcutActive: vi.fn(),
      onOpenUpdatesRequested: vi.fn(),
      onFocusSession: vi.fn(),
      onNotificationAction: vi.fn(),
      onNotificationActivate: vi.fn(),
      onDeepLink: vi.fn(),
      signalDeepLinkReady: vi.fn(),
      onClosePreviewRequested: vi.fn(),
      onOpenFolderRequested: vi.fn()
    } as unknown as Window['hermesDesktop']
  })

  afterEach(() => {
    vi.useRealTimers()
    if (initialHermesDesktop) {
      desktopWindow.hermesDesktop = initialHermesDesktop
    }

    vi.restoreAllMocks()
  })

  function render({
    activeProfile = 'default',
    locationPathname = '/',
    profileReady = false,
    restoreScopeOverride = undefined as ConversationRestoreScope | undefined,
    routedSessionId = null as string | null,
    sessions = [] as readonly SessionInfo[]
  } = {}) {
    return renderHook(
      ({
        activeProfile,
        locationPathname,
        profileReady,
        restoreScopeOverride,
        routedSessionId,
        sessions
      }: {
        activeProfile: string
        locationPathname: string
        profileReady: boolean
        restoreScopeOverride?: ConversationRestoreScope
        routedSessionId: string | null
        sessions: readonly SessionInfo[]
      }) =>
        useDesktopIntegrations({
          activeSessionId: null,
          activeProfile,
          chatOpen: false,
          creatingSessionRef: { current: false },
          gatewayState: 'open',
          hasPreview: false,
          locationPathname,
          navigate,
          profileReady,
          refreshSessions: vi.fn(),
          routedSessionId,
          restoreScope: restoreScopeOverride ?? {
            activationEpoch: gatewayActivationEpoch(),
            connection: { mode: 'local', profile: activeProfile } as HermesConnection,
            connectionId: null,
            gatewayScope: `\0${activeProfile}`,
            profile: activeProfile,
            storageSuffix: ''
          },
          runtimeIdByStoredSessionId: { current: new Map() },
          sessions,
          selectedStoredSessionId: null
        }),
      {
        initialProps: {
          activeProfile,
          locationPathname,
          profileReady,
          restoreScopeOverride,
          routedSessionId,
          sessions
        }
      }
    )
  }

  describe('profile-ready gate', () => {
    it('does NOT restore before profileReady is true', () => {
      // Set remembered state, but profileReady=false.
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.default', '/remembered-session')
      window.localStorage.setItem('hermes.desktop.lastSessionId.profile.default', 'remembered-session')

      render({ profileReady: false })

      // no navigation should have occurred
      expect(navigate).not.toHaveBeenCalled()
    })

    it('restores on profileReady when the authoritative lookup finds the remembered route', async () => {
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.default', '/remembered-session')

      const sessions = [session({ id: 'remembered-session', profile: 'default' })]

      render({ profileReady: true, sessions })

      await waitFor(() => expect(navigate).toHaveBeenCalledWith('/remembered-session', { replace: true }))
    })

    it('restores remembered session id when no remembered route exists', async () => {
      window.localStorage.setItem('hermes.desktop.lastSessionId.profile.default', 'remembered-session')

      const sessions = [session({ id: 'remembered-session', profile: 'default' })]

      render({ profileReady: true, sessions })

      // sessionRoute('remembered-session') = '/remembered-session'
      await waitFor(() => expect(navigate).toHaveBeenCalledWith('/remembered-session', { replace: true }))
    })

    it('preserves the remembered route when authoritative lookup is inconclusive', async () => {
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.default', '/remembered-session')

      restoreLookupMock.mockResolvedValue({ status: 'inconclusive', reason: 'network' })
      vi.useFakeTimers()
      render({ profileReady: true, sessions: [] })

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500)
      })

      expect(navigate).not.toHaveBeenCalled()
      expect(window.localStorage.getItem('hermes.desktop.lastRoute.profile.default')).toBe('/remembered-session')
    })
  })

  describe('ownership validation', () => {
    it('refuses to restore when exact lookup reports a conflicting owner', async () => {
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.default', '/ai-session')

      const sessions = [session({ id: 'ai-session', profile: 'ai-engineer' })]

      restoreLookupMock.mockResolvedValue({
        status: 'inconclusive',
        reason: 'session response belongs to another connection'
      })
      vi.useFakeTimers()
      render({ activeProfile: 'default', profileReady: true, sessions })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500)
      })
      expect(navigate).not.toHaveBeenCalled()
    })

    it('refuses to restore a session id owned by another profile', () => {
      window.localStorage.setItem('hermes.desktop.lastSessionId.profile.default', 'ai-session')
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.default', '/ai-session')

      const sessions = [session({ id: 'ai-session', profile: 'ai-engineer' })]

      render({ activeProfile: 'default', profileReady: true, sessions })

      // Both route and fallback session id are owned by another profile.
      expect(navigate).not.toHaveBeenCalled()
    })

    it('restores a remembered route owned by the active profile', async () => {
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.ai-engineer', '/ai-session')

      const sessions = [session({ id: 'ai-session', profile: 'ai-engineer' })]

      render({ activeProfile: 'ai-engineer', profileReady: true, sessions })

      // The route and session match the active profile — should restore.
      await waitFor(() => expect(navigate).toHaveBeenCalledWith('/ai-session', { replace: true }))
    })
  })

  describe('two profiles with distinct sessions', () => {
    it('restores profile A session when profile A is active', async () => {
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.coder', '/coder-session')

      const sessions = [
        session({ id: 'coder-session', profile: 'coder' }),
        session({ id: 'ops-session', profile: 'ops' })
      ]

      render({ activeProfile: 'coder', profileReady: true, sessions })

      await waitFor(() => expect(navigate).toHaveBeenCalledWith('/coder-session', { replace: true }))
    })

    it('does NOT bleed profile A session into profile B', () => {
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.coder', '/coder-session')

      const sessions = [session({ id: 'coder-session', profile: 'coder' })]

      // ops profile is active but has no own remembered route
      render({
        activeProfile: 'ops',
        profileReady: true,
        sessions
      })

      // No navigation — coder's remembered route doesn't belong to ops.
      expect(navigate).not.toHaveBeenCalled()
    })
  })

  describe('HUD window (win=hud)', () => {
    beforeEach(() => {
      hudWindowMock.mockReturnValue(true)
    })

    it('does NOT restore remembered navigation on a blank new-chat route', () => {
      window.localStorage.setItem('hermes.desktop.lastSessionId.profile.default', 'remembered-session')
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.default', '/remembered-session')

      render({ profileReady: true, sessions: [session({ id: 'remembered-session', profile: 'default' })] })

      // The HUD is a fresh full renderer booting at the default route, but its
      // destination was chosen explicitly by hudTargetSessionId() at open time
      // — remembered-navigation restore must not hijack it to the last session.
      expect(navigate).not.toHaveBeenCalled()
    })

    it('does NOT write remembered navigation while showing a session', () => {
      render({
        profileReady: true,
        routedSessionId: 'live',
        sessions: [session({ id: 'live', profile: 'default' })]
      })

      expect(window.localStorage.getItem('hermes.desktop.lastSessionId.profile.default')).toBeNull()
      expect(window.localStorage.getItem('hermes.desktop.lastRoute.profile.default')).toBeNull()
    })

    it('does not restore the remembered session id either', () => {
      window.localStorage.setItem('hermes.desktop.lastSessionId.profile.default', 'remembered-session')

      render({ profileReady: true, sessions: [session({ id: 'remembered-session', profile: 'default' })] })

      expect(navigate).not.toHaveBeenCalled()
    })
  })

  describe('legacy key behavior', () => {
    it('discards legacy global keys on read and does NOT restore from them', () => {
      // Simulate a pre-per-profile install.
      window.localStorage.setItem('hermes.desktop.lastSessionId', 'legacy-session')
      window.localStorage.setItem('hermes.desktop.lastRoute', '/session/legacy-session')

      // Profile contexts without matching sessions.
      const sessions = [session({ id: 'legacy-session', profile: 'default' })]

      render({ profileReady: true, sessions })

      // Legacy keys must be discarded.
      expect(window.localStorage.getItem('hermes.desktop.lastSessionId')).toBeNull()
      expect(window.localStorage.getItem('hermes.desktop.lastRoute')).toBeNull()

      // And no navigation should happen (the per-profile keys were empty).
      expect(navigate).not.toHaveBeenCalled()
    })
  })

  describe('stale-result suppression during profile switch', () => {
    it('remembers route for the new profile after switch, not the old one', () => {
      const sessions = [
        session({ id: 'coder-session', profile: 'coder' }),
        session({ id: 'ops-session', profile: 'ops' })
      ]

      // Render with coder active and navigate to a session.
      const { rerender } = render({
        activeProfile: 'coder',
        locationPathname: '/coder-session',
        profileReady: true,
        routedSessionId: 'coder-session',
        sessions
      })

      // The coder session should be persisted under coder's key.
      expect(window.localStorage.getItem('hermes.desktop.lastSessionId.profile.coder')).toBe('coder-session')

      // Now switch to ops.
      rerender({
        activeProfile: 'ops',
        locationPathname: '/ops-session',
        profileReady: true,
        restoreScopeOverride: undefined,
        routedSessionId: 'ops-session',
        sessions
      })

      // The ops session should now be persisted under ops's key.
      expect(window.localStorage.getItem('hermes.desktop.lastSessionId.profile.ops')).toBe('ops-session')

      // Coder's remembered session should still be there.
      expect(window.localStorage.getItem('hermes.desktop.lastSessionId.profile.coder')).toBe('coder-session')
    })

    it('does NOT overwrite remembered state when session ownership fails validation', () => {
      // Simulate an async restore result arriving for a route that doesn't
      // own the active profile.
      const sessions = [session({ id: 'coder-session', profile: 'coder' })]

      // Active profile is ops, but the routed session belongs to coder.
      render({
        activeProfile: 'ops',
        locationPathname: '/',
        profileReady: true,
        routedSessionId: 'coder-session', // wrong profile!
        sessions
      })

      // No session should be remembered for the active profile.
      expect(window.localStorage.getItem('hermes.desktop.lastSessionId.profile.ops')).toBeNull()
    })
  })

  describe('route-scoped restoration', () => {
    it('restores a non-session route like /skills', () => {
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.default', '/skills')

      const sessions = [session({ id: 'some-session', profile: 'default' })]

      render({ profileReady: true, sessions })

      // /skills is not a session route — no ownership validation needed.
      expect(navigate).toHaveBeenCalledWith('/skills', { replace: true })
    })

    it('does NOT restore overlay routes (settings/command-center)', () => {
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.default', '/settings')

      render({ profileReady: true, sessions: [] })

      // Overlay routes should not be restored.
      expect(navigate).not.toHaveBeenCalled()
    })

    it('does NOT persist overlay routes for next boot', () => {
      const { rerender } = render({
        activeProfile: 'default',
        locationPathname: '/settings',
        profileReady: true,
        routedSessionId: null,
        sessions: []
      })

      // Remembering effect fires on route change.
      rerender({
        activeProfile: 'default',
        locationPathname: '/settings',
        profileReady: true,
        restoreScopeOverride: undefined,
        routedSessionId: null,
        sessions: []
      })

      // Overlay routes must NOT be persisted.
      expect(window.localStorage.getItem('hermes.desktop.lastRoute.profile.default')).toBeNull()
    })
  })

  describe('resume exhaustion persistence', () => {
    it('preserves rollback navigation because generic exhaustion is not authoritative deletion proof', () => {
      window.localStorage.setItem('hermes.desktop.lastSessionId.profile.default', 'exhausted')
      window.localStorage.setItem('hermes.desktop.lastRoute.profile.default', '/exhausted')

      render({ profileReady: true, sessions: [session({ id: 'exhausted', profile: 'default' })] })

      expect(window.localStorage.getItem('hermes.desktop.lastSessionId.profile.default')).toBe('exhausted')
      expect(window.localStorage.getItem('hermes.desktop.lastRoute.profile.default')).toBe('/exhausted')
    })
  })

  describe('notification activate + plugin deep links', () => {
    it('navigates when a plugin notification activate payload arrives', () => {
      let activate: ((payload: { activate?: string }) => void) | undefined
      desktopWindow.hermesDesktop = {
        ...desktopWindow.hermesDesktop,
        onNotificationActivate: (cb: (payload: { activate?: string }) => void) => {
          activate = cb

          return () => undefined
        }
      } as unknown as Window['hermesDesktop']

      render({ profileReady: true, sessions: [] })
      activate?.({ activate: '/index-network/intent/1' })
      expect(navigate).toHaveBeenCalledWith('/index-network/intent/1')
    })

    it('navigates hermes://index-network/intent/1 deep links through the same path vocabulary', () => {
      let deepLink: ((payload: { kind: string; name: string; params: Record<string, string> }) => void) | undefined
      desktopWindow.hermesDesktop = {
        ...desktopWindow.hermesDesktop,
        onDeepLink: (cb: (payload: { kind: string; name: string; params: Record<string, string> }) => void) => {
          deepLink = cb

          return () => undefined
        },
        signalDeepLinkReady: vi.fn()
      } as unknown as Window['hermesDesktop']

      render({ profileReady: true, sessions: [] })
      deepLink?.({ kind: 'index-network', name: 'intent/1', params: {} })
      expect(navigate).toHaveBeenCalledWith('/index-network/intent/1')
    })

    it('routes hermes://mcp/install to the pending-install dialog, not navigation', () => {
      let deepLink: ((payload: { kind: string; name: string; params: Record<string, string> }) => void) | undefined
      desktopWindow.hermesDesktop = {
        ...desktopWindow.hermesDesktop,
        onDeepLink: (cb: (payload: { kind: string; name: string; params: Record<string, string> }) => void) => {
          deepLink = cb

          return () => undefined
        },
        signalDeepLinkReady: vi.fn()
      } as unknown as Window['hermesDesktop']

      render({ profileReady: true, sessions: [] })
      deepLink?.({ kind: 'mcp', name: 'install', params: { name: 'context7' } })
      expect(requestMcpInstallFromDeepLink).toHaveBeenCalledWith({ name: 'context7' })
      expect(navigate).not.toHaveBeenCalled()
    })
  })

  describe('profile conversation restore transaction', () => {
    it('restores a committed automatic draft through the authoritative lookup without overwriting memory', async () => {
      setRememberedConversation({ kind: 'session', sessionId: 'work-last', version: 1 }, 'work')
      const sequence = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
      commitProfileConversationRestore(sequence)
      applyFreshDraftProvenance({
        cause: 'profile-switch',
        freshSequence: 1,
        kind: 'automatic',
        restoreSequence: sequence
      })
      restoreLookupMock.mockResolvedValueOnce({
        status: 'found',
        session: session({ id: 'work-last', profile: 'work' })
      })

      render({ activeProfile: 'work', profileReady: true })

      await waitFor(() => expect(navigate).toHaveBeenCalledWith('/work-last', { replace: true }))
      expect(getRememberedConversation('work')).toEqual({ kind: 'session', sessionId: 'work-last', version: 1 })
      expect($profileConversationRestore.get()).toMatchObject({ phase: 'navigating', sequence })
    })

    it('treats the explicit local descriptor as the same owner as a profile-door target', async () => {
      setRememberedConversation({ kind: 'session', sessionId: 'local-last', version: 1 }, 'work')
      const sequence = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
      commitProfileConversationRestore(sequence)
      applyFreshDraftProvenance({
        cause: 'profile-switch',
        freshSequence: 1,
        kind: 'automatic',
        restoreSequence: sequence
      })
      restoreLookupMock.mockResolvedValueOnce({
        status: 'found',
        session: session({ connection_id: 'local', id: 'local-last', profile: 'work' })
      })

      render({
        activeProfile: 'work',
        profileReady: true,
        restoreScopeOverride: {
          activationEpoch: gatewayActivationEpoch(),
          connection: {
            connectionId: 'local',
            mode: 'local'
          } as HermesConnection,
          connectionId: 'local',
          gatewayScope: `local\0work`,
          profile: 'work',
          storageSuffix: ''
        }
      })

      await waitFor(() => expect(navigate).toHaveBeenCalledWith('/local-last', { replace: true }))
      expect(restoreLookupMock).toHaveBeenCalledWith('local-last', {
        connectionId: null,
        profile: 'work',
        storageSuffix: ''
      })
      expect(publishRestoreMock).toHaveBeenCalledWith(expect.objectContaining({ id: 'local-last' }), 'local-last', {
        connectionId: null,
        profile: 'work',
        storageSuffix: ''
      })
    })

    it('rejects an explicit conflicting profile on the local descriptor', async () => {
      vi.useFakeTimers()
      const sequence = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
      commitProfileConversationRestore(sequence)
      applyFreshDraftProvenance({
        cause: 'profile-switch',
        freshSequence: 1,
        kind: 'automatic',
        restoreSequence: sequence
      })

      render({
        activeProfile: 'work',
        profileReady: true,
        restoreScopeOverride: {
          activationEpoch: gatewayActivationEpoch(),
          connection: {
            connectionId: 'local',
            mode: 'local',
            profile: 'previous'
          } as HermesConnection,
          connectionId: 'local',
          gatewayScope: `local\0work`,
          profile: 'work',
          storageSuffix: ''
        }
      })

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500)
      })

      expect(restoreLookupMock).not.toHaveBeenCalled()
      expect(navigate).not.toHaveBeenCalled()
    })

    it('rejects a retained registry descriptor for a true null/profile-door target', async () => {
      vi.useFakeTimers()
      const sequence = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
      commitProfileConversationRestore(sequence)
      applyFreshDraftProvenance({
        cause: 'profile-switch',
        freshSequence: 1,
        kind: 'automatic',
        restoreSequence: sequence
      })

      render({
        activeProfile: 'work',
        profileReady: true,
        restoreScopeOverride: {
          activationEpoch: gatewayActivationEpoch(),
          connection: {
            connectionId: 'previous-source',
            mode: 'remote',
            profile: 'work',
            registryScoped: true
          } as HermesConnection,
          connectionId: 'previous-source',
          gatewayScope: `previous-source\0work`,
          profile: 'work',
          storageSuffix: ''
        }
      })

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500)
      })

      expect(restoreLookupMock).not.toHaveBeenCalled()
      expect(navigate).not.toHaveBeenCalled()
    })

    it('durably records an explicit blank while an automatic isolation blank remains non-durable', async () => {
      applyFreshDraftProvenance({ cause: 'profile-switch', freshSequence: 1, kind: 'automatic' })
      const automatic = render({ activeProfile: 'work', profileReady: true })

      expect(getRememberedConversation('work')).toBeNull()
      automatic.unmount()

      applyFreshDraftProvenance({ cause: 'new-chat', freshSequence: 2, kind: 'explicit' })
      render({ activeProfile: 'work', profileReady: true })

      await waitFor(() => expect(getRememberedConversation('work')).toEqual({ kind: 'blank', version: 1 }))
      expect(window.localStorage.getItem('hermes.desktop.lastSessionId.profile.work')).toBeNull()
      expect(window.localStorage.getItem('hermes.desktop.lastRoute.profile.work')).toBe('/')
    })

    it('bounds inconclusive retries to 500/1000ms and preserves the remembered session', async () => {
      vi.useFakeTimers()
      setRememberedConversation({ kind: 'session', sessionId: 'retry-me', version: 1 }, 'work')
      const sequence = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
      commitProfileConversationRestore(sequence)
      applyFreshDraftProvenance({
        cause: 'profile-switch',
        freshSequence: 1,
        kind: 'automatic',
        restoreSequence: sequence
      })
      restoreLookupMock.mockResolvedValue({ status: 'inconclusive', reason: 'network' })

      render({ activeProfile: 'work', profileReady: true })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500)
      })

      expect(restoreLookupMock).toHaveBeenCalledTimes(3)
      expect(getRememberedConversation('work')).toEqual({ kind: 'session', sessionId: 'retry-me', version: 1 })
      expect($profileConversationRestore.get()).toBeNull()
      expect(vi.getTimerCount()).toBe(0)
      vi.useRealTimers()
    })

    it('lets a rapid B to C switch strand B lookup completion so only C navigates', async () => {
      setRememberedConversation({ kind: 'session', sessionId: 'b-last', version: 1 }, 'b')
      setRememberedConversation({ kind: 'session', sessionId: 'c-last', version: 1 }, 'c')
      let resolveB!: (value: unknown) => void
      const pendingB = new Promise(resolve => {
        resolveB = resolve
      })
      restoreLookupMock
        .mockImplementationOnce(() => pendingB)
        .mockResolvedValueOnce({ status: 'found', session: session({ id: 'c-last', profile: 'c' }) })

      const b = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'b' })
      commitProfileConversationRestore(b)
      applyFreshDraftProvenance({
        cause: 'profile-switch',
        freshSequence: 1,
        kind: 'automatic',
        restoreSequence: b
      })
      const result = render({ activeProfile: 'b', profileReady: true })
      await waitFor(() => expect(restoreLookupMock).toHaveBeenCalledTimes(1))

      const c = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'c' })
      commitProfileConversationRestore(c)
      applyFreshDraftProvenance({
        cause: 'profile-switch',
        freshSequence: 2,
        kind: 'automatic',
        restoreSequence: c
      })
      result.rerender({
        activeProfile: 'c',
        locationPathname: '/',
        profileReady: true,
        restoreScopeOverride: undefined,
        routedSessionId: null,
        sessions: []
      })

      await waitFor(() => expect(navigate).toHaveBeenCalledWith('/c-last', { replace: true }))
      await act(async () => {
        resolveB({ status: 'found', session: session({ id: 'b-last', profile: 'b' }) })
        await pendingB
      })
      expect(navigate).not.toHaveBeenCalledWith('/b-last', expect.anything())
      expect(publishRestoreMock).not.toHaveBeenCalledWith(
        expect.objectContaining({ id: 'b-last' }),
        'b-last',
        expect.anything()
      )
    })

    it('cancels a pending restore before explicit notification navigation', () => {
      const sequence = beginProfileConversationRestore('profile-switch', { connectionId: null, profile: 'work' })
      commitProfileConversationRestore(sequence)
      let activate: ((payload: { activate?: string; notifyId: string }) => void) | undefined
      desktopWindow.hermesDesktop = {
        ...desktopWindow.hermesDesktop,
        onNotificationActivate: (
          cb: (payload: { actionId?: string; activate?: string; notifyId?: string; tag?: string }) => void
        ) => {
          activate = cb
          return () => undefined
        }
      } as unknown as Window['hermesDesktop']

      render({ activeProfile: 'work', profileReady: true })
      activate?.({ activate: '/skills', notifyId: 'n1' })
      expect($profileConversationRestore.get()).toBeNull()
      expect(navigate).toHaveBeenCalledWith('/skills')
    })
  })
})
