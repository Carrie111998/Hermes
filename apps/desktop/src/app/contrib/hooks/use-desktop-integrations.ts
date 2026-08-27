import { useStore } from '@nanostores/react'
import { LOCAL_CONNECTION_ID, type ConnectionState } from '@hermes/shared'
import { type MutableRefObject, useEffect, useRef } from 'react'

import { closeActiveTab } from '@/app/chat/close-tab'
import { commandFocusedPreview } from '@/app/chat/right-rail/preview-nav'
import { openSession } from '@/app/open-session'
import { resolveDeepLinkAction } from '@/lib/deeplink-routes'
import { pathFromHermesDeepLink, resolveHermesOpenPath } from '@/lib/hermes-open-target'
import { storedSessionIdForNotification } from '@/lib/session-ids'
import { activeConnectionScopeSuffix } from '@/lib/connection-scoped'
import { gatewayActivationEpoch } from '@/store/gateway'
import { requestMcpInstallFromDeepLink } from '@/store/mcp-deeplink-install'
import { startMcpHealthChecker, stopMcpHealthChecker } from '@/store/mcp-health'
import {
  clearPluginNotifyHandlers,
  invokePluginNotifyAction,
  invokePluginNotifyActivate,
  respondToApprovalAction
} from '@/store/native-notifications'
import { openPluginInstallRequest } from '@/store/plugin-install-request'
import { openFolderAsProject } from '@/store/projects'
import { normalizeProfileKey } from '@/store/profile'
import {
  $appliedFreshDraftProvenance,
  $profileConversationRestore,
  cancelProfileConversationRestore,
  clearAppliedFreshDraftProvenance,
  completeProfileConversationRestore,
  isCurrentProfileConversationRestore,
  markProfileConversationRestoreNavigating,
  type ProfileConversationRestoreRequest
} from '@/store/profile-conversation-restore'
import {
  clearRememberedConversationIfSession,
  getRememberedConversation,
  getRememberedRoute,
  requestSessionResume,
  sessionBelongsToProfile,
  setRememberedConversation,
  setRememberedRoute
} from '@/store/session'
import { onSessionsChanged } from '@/store/session-sync'
import { openUpdatesWindow, startUpdatePoller, stopUpdatePoller } from '@/store/updates'
import { isBrowserWindow, isHudWindow, isSecondaryWindow } from '@/store/windows'
import type { SessionInfo } from '@/types/hermes'
import type { HermesConnection } from '@/global'

import { requestComposerFocus, requestComposerInsert } from '../../chat/composer/focus'
import { appViewForPath, isOverlayView, NEW_CHAT_ROUTE, routeSessionId, sessionRoute } from '../../routes'
import {
  publishResolvedSessionForRestore,
  resolveStoredSessionForRestore,
  type RestoreLookupResult
} from '../../session/hooks/use-session-actions/utils'

type RememberedSession = Pick<SessionInfo, '_lineage_root_id' | 'connection_id' | 'id' | 'profile'>

export interface ConversationRestoreScope {
  activationEpoch: number
  connection: HermesConnection | null
  connectionId: null | string
  gatewayScope: string
  profile: string
  storageSuffix: string
}

interface DesktopIntegrationsParams {
  activeSessionId: null | string
  activeProfile: string
  chatOpen: boolean
  creatingSessionRef: MutableRefObject<boolean>
  gatewayState: ConnectionState
  hasPreview: boolean
  locationPathname: string
  navigate: (to: string, options?: { replace?: boolean }) => void
  profileReady: boolean
  refreshSessions: () => Promise<unknown> | unknown
  routedSessionId: null | string
  restoreScope: ConversationRestoreScope
  runtimeIdByStoredSessionId: { readonly current: Map<string, string> }
  sessions: readonly RememberedSession[]
  selectedStoredSessionId: null | string
}

interface RestoreAttemptControl {
  cancelled: boolean
  timers: Set<ReturnType<typeof setTimeout>>
}

function cancelRestoreAttempt(control: RestoreAttemptControl): void {
  control.cancelled = true
  for (const timer of control.timers) {
    clearTimeout(timer)
  }
  control.timers.clear()
}

function restoreDelay(control: RestoreAttemptControl, milliseconds: number): Promise<void> {
  return new Promise(resolve => {
    const timer = setTimeout(() => {
      control.timers.delete(timer)
      resolve()
    }, milliseconds)
    control.timers.add(timer)
  })
}

async function resolveRememberedConversation(
  sessionId: string,
  scope: ConversationRestoreScope,
  control: RestoreAttemptControl,
  isValid: () => boolean
): Promise<RestoreLookupResult | null> {
  const delays = [0, 500, 1_000]
  let last: RestoreLookupResult | null = null

  for (let attempt = 0; attempt < delays.length; attempt += 1) {
    if (attempt > 0) {
      await restoreDelay(control, delays[attempt])
    }

    if (control.cancelled || !isValid()) {
      return null
    }

    last = await resolveStoredSessionForRestore(sessionId, {
      connectionId: scope.connectionId,
      profile: scope.profile,
      storageSuffix: scope.storageSuffix
    })

    if (control.cancelled || !isValid() || last.status !== 'inconclusive') {
      return control.cancelled || !isValid() ? null : last
    }
  }

  return last
}

function scopeMatchesRequest(scope: ConversationRestoreScope, request: ProfileConversationRestoreRequest): boolean {
  const profile = normalizeProfileKey(scope.profile)
  const descriptorProfile = scope.connection?.profile?.trim()

  if (
    scope.gatewayScope !== `${scope.connectionId ?? ''}\0${profile}` ||
    profile !== normalizeProfileKey(request.target.profile) ||
    (descriptorProfile && normalizeProfileKey(descriptorProfile) !== profile) ||
    scope.storageSuffix !== activeConnectionScopeSuffix()
  ) {
    return false
  }

  if (request.target.connectionId) {
    return (
      scope.connectionId === request.target.connectionId &&
      scope.connection?.connectionId === request.target.connectionId &&
      scope.connection.registryScoped === true
    )
  }

  // A profile-door activation is represented as null by the restore request,
  // while the main process stamps the live local descriptor with the explicit
  // `local` registry id. They are the same owner. Still fail closed for any
  // retained remote/registry descriptor from the previously active source.
  const scopeConnectionId = scope.connectionId?.trim() || null
  const descriptorConnectionId = scope.connection?.connectionId?.trim() || null
  const localDoor = (value: null | string) => value === null || value === LOCAL_CONNECTION_ID

  return localDoor(scopeConnectionId) && localDoor(descriptorConnectionId) && scope.connection?.registryScoped !== true
}

function scopeDescriptorIsSettled(scope: ConversationRestoreScope): boolean {
  const profile = normalizeProfileKey(scope.profile)
  const descriptorProfile = scope.connection?.profile?.trim()

  return (
    scope.gatewayScope === `${scope.connectionId ?? ''}\0${profile}` &&
    (!descriptorProfile || normalizeProfileKey(descriptorProfile) === profile) &&
    (scope.connectionId
      ? scope.connection?.connectionId === scope.connectionId
      : !scope.connection?.connectionId && scope.connection?.registryScoped !== true) &&
    scope.storageSuffix === activeConnectionScopeSuffix()
  )
}

function sessionOwnedByScope(
  sessions: readonly RememberedSession[],
  sessionId: string,
  scope: ConversationRestoreScope
): boolean {
  return sessions.some(row => {
    const identityMatches = row.id === sessionId || (row._lineage_root_id ?? row.id) === sessionId
    const profileMatches = normalizeProfileKey(row.profile) === normalizeProfileKey(scope.profile)
    const connectionMatches = scope.connectionId ? row.connection_id === scope.connectionId : !row.connection_id

    return identityMatches && profileMatches && connectionMatches
  })
}

/**
 * All the Electron-main / OS / cross-window integrations the shell listens for:
 * update polling, the ⌘W close shortcut, deep links, native-notification
 * navigation, preview-shortcut enablement, remembered-session restore, and
 * cross-window session-list sync. Kept out of the wiring controller so the
 * "talks to the desktop shell" surface reads as one unit.
 */
export function useDesktopIntegrations({
  activeSessionId,
  activeProfile,
  creatingSessionRef,
  gatewayState,
  locationPathname,
  navigate,
  profileReady,
  refreshSessions,
  routedSessionId,
  restoreScope,
  runtimeIdByStoredSessionId,
  sessions,
  selectedStoredSessionId
}: DesktopIntegrationsParams): void {
  // Update polling — populates $desktopVersion/$updateStatus, which feed the
  // statusbar version pill and the update toasts. Also honors the main
  // process's "open updates" menu request.
  useEffect(() => {
    startUpdatePoller()
    // Background MCP health: HTTP/SSE servers only (never spawns stdio),
    // notifies on transitions into needs-auth/error with a Sign in action.
    startMcpHealthChecker()
    const unsubscribe = window.hermesDesktop?.onOpenUpdatesRequested?.(() => openUpdatesWindow())

    return () => {
      unsubscribe?.()
      stopUpdatePoller()
      stopMcpHealthChecker()
    }
  }, [])

  // The renderer OWNS ⌘W: on macOS the native menu accelerator would else
  // close the window, so claim it unconditionally — the menu then routes ⌘W
  // to us (close-preview-requested IPC) and we decide tab-vs-window.
  useEffect(() => {
    window.hermesDesktop?.setPreviewShortcutActive?.(true)
  }, [])

  const restoreRequest = useStore($profileConversationRestore)
  const appliedProvenance = useStore($appliedFreshDraftProvenance)
  const restoredRef = useRef(false)
  const latestRef = useRef({
    activeSessionId,
    appliedProvenance,
    creatingSessionRef,
    gatewayState,
    locationPathname,
    restoreRequest,
    restoreScope,
    selectedStoredSessionId
  })

  latestRef.current = {
    activeSessionId,
    appliedProvenance,
    creatingSessionRef,
    gatewayState,
    locationPathname,
    restoreRequest,
    restoreScope,
    selectedStoredSessionId
  }

  // A pathname change is a backup cancellation boundary. Real user actions
  // cancel synchronously at their open/navigation/submit door; this observer
  // catches browser/deep-link paths while recognizing the route owned by a
  // restore transaction itself.
  useEffect(() => {
    const current = $profileConversationRestore.get()

    if (!current) {
      if (locationPathname !== NEW_CHAT_ROUTE) {
        clearAppliedFreshDraftProvenance()
      }
      return
    }

    if (current.phase === 'navigating' && routeSessionId(locationPathname) === current.sessionId) {
      clearAppliedFreshDraftProvenance()
      completeProfileConversationRestore(current.sequence)
      return
    }

    const provenance = $appliedFreshDraftProvenance.get()
    const ownsBlank =
      provenance?.kind === 'automatic' &&
      provenance.restoreSequence === current.sequence &&
      locationPathname === NEW_CHAT_ROUTE

    if (
      (current.phase === 'activating' || current.phase === 'committed' || current.phase === 'navigating') &&
      !ownsBlank
    ) {
      cancelProfileConversationRestore(current.sequence, 'pathname-changed')
      clearAppliedFreshDraftProvenance()
    }
  }, [locationPathname])

  // Cold restore and live restore share one cancellation boundary through this
  // dependency: beginning any live request tears down the cold attempt before
  // it can publish navigation. Cold retains the historic non-overlay route
  // policy; live switching restores conversations only.
  useEffect(() => {
    if (!profileReady || isHudWindow() || isBrowserWindow()) {
      return
    }

    const control: RestoreAttemptControl = { cancelled: false, timers: new Set() }
    const live = restoreRequest

    const exactScopeStillCurrent = (captured: ConversationRestoreScope): boolean => {
      const latest = latestRef.current

      return (
        !control.cancelled &&
        latest.gatewayState === 'open' &&
        latest.restoreScope.activationEpoch === captured.activationEpoch &&
        latest.restoreScope.connectionId === captured.connectionId &&
        latest.restoreScope.gatewayScope === captured.gatewayScope &&
        latest.restoreScope.profile === captured.profile &&
        latest.restoreScope.storageSuffix === captured.storageSuffix &&
        activeConnectionScopeSuffix() === captured.storageSuffix
      )
    }

    const runColdRestore = async () => {
      if (restoredRef.current) {
        return
      }

      if (locationPathname !== NEW_CHAT_ROUTE) {
        restoredRef.current = true
        return
      }

      const captured = restoreScope

      if (
        gatewayState !== 'open' ||
        captured.activationEpoch !== gatewayActivationEpoch() ||
        !scopeDescriptorIsSettled(captured)
      ) {
        return
      }

      const rememberedRoute = getRememberedRoute(captured.profile)
      const rememberedRouteSession = rememberedRoute ? routeSessionId(rememberedRoute) : null
      const restorablePage =
        !!rememberedRoute &&
        rememberedRoute !== NEW_CHAT_ROUTE &&
        !rememberedRouteSession &&
        !isOverlayView(appViewForPath(rememberedRoute))

      if (restorablePage) {
        if (exactScopeStillCurrent(captured) && !$profileConversationRestore.get()) {
          restoredRef.current = true
          clearAppliedFreshDraftProvenance()
          navigate(rememberedRoute, { replace: true })
        }
        return
      }

      const remembered = getRememberedConversation(captured.profile)

      if (remembered?.kind === 'blank') {
        restoredRef.current = true
        return
      }

      const candidate = remembered?.kind === 'session' ? remembered.sessionId : null

      if (!candidate) {
        restoredRef.current = true
        return
      }

      const valid = () => exactScopeStillCurrent(captured) && !$profileConversationRestore.get()
      const result = await resolveRememberedConversation(candidate, captured, control, valid)

      if (!result || !valid()) {
        return
      }

      restoredRef.current = true

      if (result.status === 'found') {
        publishResolvedSessionForRestore(result.session, candidate, captured)
        setRememberedConversation({ kind: 'session', sessionId: candidate, version: 1 }, captured.profile)
        requestSessionResume(candidate, result.ownerRoute, { forceCold: true })
        clearAppliedFreshDraftProvenance()
        navigate(sessionRoute(candidate), { replace: true })
      } else if (result.status === 'not-found') {
        clearRememberedConversationIfSession(captured.profile, candidate)
      } else {
        console.warn(
          `[desktop] remembered conversation lookup remained inconclusive for ${captured.gatewayScope}`,
          result.reason
        )
      }
    }

    const runLiveRestore = async (request: ProfileConversationRestoreRequest) => {
      if (request.phase !== 'committed') {
        return
      }

      // Commit and the passive fresh-draft consumer can settle in either order.
      // Give the exact scope/provenance three bounded opportunities to converge.
      let captured: ConversationRestoreScope | null = null
      for (const delay of [0, 500, 1_000]) {
        if (delay) {
          await restoreDelay(control, delay)
        }

        const latest = latestRef.current
        const current = $profileConversationRestore.get()
        const provenance = $appliedFreshDraftProvenance.get()
        const candidateScope = latest.restoreScope
        const ready =
          current?.sequence === request.sequence &&
          current.phase === 'committed' &&
          latest.gatewayState === 'open' &&
          candidateScope.activationEpoch === gatewayActivationEpoch() &&
          scopeMatchesRequest(candidateScope, current) &&
          provenance?.kind === 'automatic' &&
          provenance.restoreSequence === request.sequence &&
          latest.locationPathname === NEW_CHAT_ROUTE &&
          !latest.activeSessionId &&
          !latest.selectedStoredSessionId &&
          !latest.creatingSessionRef.current

        if (ready) {
          captured = candidateScope
          break
        }

        if (control.cancelled || !isCurrentProfileConversationRestore(request.sequence)) {
          return
        }
      }

      if (!captured) {
        completeProfileConversationRestore(request.sequence)
        return
      }

      const valid = () => {
        const latest = latestRef.current
        const current = $profileConversationRestore.get()
        const provenance = $appliedFreshDraftProvenance.get()

        return (
          exactScopeStillCurrent(captured!) &&
          current?.sequence === request.sequence &&
          current.phase === 'committed' &&
          scopeMatchesRequest(latest.restoreScope, current) &&
          provenance?.kind === 'automatic' &&
          provenance.restoreSequence === request.sequence &&
          latest.locationPathname === NEW_CHAT_ROUTE &&
          !latest.activeSessionId &&
          !latest.selectedStoredSessionId &&
          !latest.creatingSessionRef.current
        )
      }

      const remembered = getRememberedConversation(captured.profile)

      if (remembered?.kind === 'blank' || !remembered) {
        completeProfileConversationRestore(request.sequence)
        return
      }

      const candidate = remembered.sessionId
      const result = await resolveRememberedConversation(candidate, captured, control, valid)

      if (!result || !valid()) {
        return
      }

      if (result.status === 'found') {
        if (!markProfileConversationRestoreNavigating(request.sequence, candidate)) {
          return
        }

        publishResolvedSessionForRestore(result.session, candidate, captured)
        setRememberedConversation({ kind: 'session', sessionId: candidate, version: 1 }, captured.profile)
        requestSessionResume(candidate, result.ownerRoute, { forceCold: true })
        navigate(sessionRoute(candidate), { replace: true })
        return
      }

      if (result.status === 'not-found') {
        clearRememberedConversationIfSession(captured.profile, candidate)
      } else {
        console.warn(
          `[desktop] profile conversation restore remained inconclusive for ${captured.gatewayScope}`,
          result.reason
        )
      }

      completeProfileConversationRestore(request.sequence)
    }

    if (live) {
      restoredRef.current = true
      void runLiveRestore(live)
    } else {
      void runColdRestore()
    }

    return () => cancelRestoreAttempt(control)
  }, [gatewayState, locationPathname, navigate, profileReady, restoreRequest, restoreScope])

  // Persist independently from restoration. The coordinator atom is read
  // synchronously so an activation cannot slip a transitional old route or `/`
  // into the target scope before React has painted the new transaction.
  useEffect(() => {
    if (
      !profileReady ||
      isHudWindow() ||
      isBrowserWindow() ||
      $profileConversationRestore.get() ||
      gatewayState !== 'open' ||
      restoreScope.activationEpoch !== gatewayActivationEpoch() ||
      !scopeDescriptorIsSettled(restoreScope)
    ) {
      return
    }

    if (
      routedSessionId &&
      sessionBelongsToProfile(sessions, routedSessionId, activeProfile) &&
      sessionOwnedByScope(sessions, routedSessionId, restoreScope)
    ) {
      setRememberedConversation({ kind: 'session', sessionId: routedSessionId, version: 1 }, activeProfile)
      return
    }

    if (routedSessionId || isOverlayView(appViewForPath(locationPathname))) {
      return
    }

    const provenance = $appliedFreshDraftProvenance.get()

    if (
      locationPathname === NEW_CHAT_ROUTE &&
      !activeSessionId &&
      !selectedStoredSessionId &&
      provenance?.kind === 'explicit'
    ) {
      setRememberedConversation({ kind: 'blank', version: 1 }, activeProfile)
      return
    }

    // `/` has no durable meaning without typed provenance. In particular,
    // cold restoration starts on `/` and may still be validating a remembered
    // conversation asynchronously; writing the route here would destroy the
    // rollback keys on an inconclusive lookup. Only the explicit branch above
    // is allowed to persist a blank preference.
    if (locationPathname === NEW_CHAT_ROUTE) {
      return
    }

    setRememberedRoute(locationPathname, activeProfile)
  }, [
    activeProfile,
    activeSessionId,
    appliedProvenance,
    gatewayState,
    locationPathname,
    profileReady,
    restoreRequest,
    restoreScope,
    routedSessionId,
    selectedStoredSessionId,
    sessions
  ])

  // Native-notification click -> jump to the session WHERE IT ALREADY IS (open
  // tile / main), else beside what's loaded rather than over it — the click
  // came from outside the app and shouldn't cost the user the chat they left
  // on screen. Runtime id is translated to the stored id the chat route is
  // keyed by; action buttons resolve in place.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onFocusSession?.(sessionId => {
      if (sessionId) {
        openSession(storedSessionIdForNotification(sessionId, runtimeIdByStoredSessionId.current), navigate, 'stack')
      }
    })

    return () => unsubscribe?.()
  }, [navigate, runtimeIdByStoredSessionId])

  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onNotificationAction?.(({ actionId, sessionId }) => {
      void respondToApprovalAction(sessionId ?? null, actionId)
    })

    return () => unsubscribe?.()
  }, [])

  // Plugin OS notification body/action → optional callback + navigate. Activation
  // is user-driven (click), so this is offer-not-hijack. Paths share the
  // hermes://index-network/intent/1 vocabulary with deep links.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onNotificationActivate?.(payload => {
      if (!payload) {
        return
      }

      if (payload.actionId) {
        invokePluginNotifyAction(payload.notifyId, payload.actionId)
      } else {
        invokePluginNotifyActivate(payload.notifyId)
      }

      if (payload.activate) {
        // Defense-in-depth: re-resolve at the IPC boundary rather than trusting
        // the pre-IPC validation — any future hermesDesktop.notify caller gets
        // funneled through the same resolver.
        const path = resolveHermesOpenPath(payload.activate)

        if (path) {
          cancelProfileConversationRestore(undefined, 'notification-navigation')
          navigate(path)
        }
      }

      clearPluginNotifyHandlers(payload.notifyId)
    })

    return () => unsubscribe?.()
  }, [navigate])

  // hermes:// deep links:
  //  - mcp/install?… → pending MCP install (explicit confirm, never auto-install)
  //  - plugin/install?… (and legacy plugin-agent/plugin-desktop) → plugin install
  //    modal awaiting explicit confirmation. Never auto-installs.
  //  - blueprint/<name>?… → reviewable /blueprint command in the composer
  //  - <plugin>/<path>?… → in-app navigate (e.g. index-network/intent/1)
  //  - open/<path>?… → in-app navigate (generic)
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onDeepLink?.(payload => {
      if (!payload?.kind) {
        return
      }

      if (payload.kind === 'mcp' && payload.name === 'install') {
        requestMcpInstallFromDeepLink(payload.params || {})

        return
      }

      const action = resolveDeepLinkAction(payload)

      if (action.type === 'composer-blueprint') {
        const slots = Object.entries(action.params || {})
          .map(([k, v]) => {
            const sval = /\s/.test(v) ? `"${v.replace(/"/g, '\\"')}"` : v

            return `${k}=${sval}`
          })
          .join(' ')

        const command = `/blueprint ${action.name}${slots ? ' ' + slots : ''}`
        requestComposerInsert(command, { mode: 'block', target: 'main' })
        requestComposerFocus('main')

        return
      }

      if (action.type === 'plugin-install') {
        openPluginInstallRequest({
          repo: action.repo,
          enable: action.enable,
          force: action.force,
          legacyHint: action.legacyHint
        })

        return
      }

      // Not a core action — treat as a plugin-scoped or open/ navigation deep
      // link (hermes://index-network/intent/1, hermes://open/…). The resolver
      // rejects reserved kinds and unsafe paths.
      const path = pathFromHermesDeepLink(payload.kind, payload.name || '', payload.params || {})

      if (path) {
        cancelProfileConversationRestore(undefined, 'deep-link-navigation')
        navigate(path)
      }
    })

    void window.hermesDesktop?.signalDeepLinkReady?.()

    return () => unsubscribe?.()
  }, [navigate])

  // ⌘W via the macOS menu accelerator → close the focused tab; if nothing is
  // closeable, fall back to closing the window (so ⌘W still works as the
  // OS-standard window close, esp. secondary windows). The Win/Linux keyboard
  // path is the `view.closeTab` keybind (use-keybinds), sharing closeActiveTab.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onClosePreviewRequested?.(
      () => void closeActiveTab(id => navigate(sessionRoute(id)))
    )

    return () => unsubscribe?.()
  }, [navigate])

  // Native browser gestures (⌘R, a mouse's back/forward buttons, a trackpad
  // swipe) that landed on the app's own chrome rather than inside a page — main
  // answers those against the focused guest and never asks. Only ⌘R has an
  // app-level meaning to fall back to; an unfocused swipe is a no-op.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onPreviewNav?.(command => {
      if (!commandFocusedPreview(command) && command === 'reload') {
        window.location.reload()
      }
    })

    return () => unsubscribe?.()
  }, [])

  // File > Open Folder… — same open-folder-as-project upsert as the ⌘O keybind.
  useEffect(() => {
    const unsubscribe = window.hermesDesktop?.onOpenFolderRequested?.(() => void openFolderAsProject())

    return () => unsubscribe?.()
  }, [])

  // Another window mutated the shared session list -> re-pull the sidebar.
  useEffect(() => {
    if (isSecondaryWindow() || isBrowserWindow()) {
      return
    }

    return onSessionsChanged(() => void refreshSessions())
  }, [refreshSessions])
}
