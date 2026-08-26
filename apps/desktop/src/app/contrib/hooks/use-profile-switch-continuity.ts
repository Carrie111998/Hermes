import { useStore } from '@nanostores/react'
import { useEffect, useRef } from 'react'

import { openSession } from '@/app/open-session'
import { findGroup, findGroupOfPane, type LayoutNode } from '@/components/pane-shell/tree/model'
import { $activeTreeGroup, $layoutTree } from '@/components/pane-shell/tree/store'
import { $pendingConnectionId } from '@/store/connections'
import { activeGatewayConnectionId, gatewayActivationEpoch } from '@/store/gateway'
import {
  $profileSwitchBehavior,
  $profileSwitchRestoreToken,
  clearProfileSwitchRestore,
  observeProfileSwitchRestoreNavigation,
  profileSwitchNavigationToken,
  registerProfileSwitchAnchorCapture,
  type ProfileSwitchActivation,
  type ProfileSwitchPaneFocus
} from '@/store/profile-switch-behavior'
import { $freshSessionRequest, profilePickConnectionId } from '@/store/profile'
import { getRememberedSessionId, requestSessionResume, setRememberedSessionId } from '@/store/session'
import { isSessionOwnerRoute, type SessionOwnerScope } from '@/store/session-request-router'
import {
  $focusedStoredSessionId,
  focusOpenSession,
  knownOwnerForSession,
  markSelectionRestore
} from '@/store/session-states'
import { isBrowserWindow, isHudWindow, isSecondaryWindow } from '@/store/windows'

interface ProfileSwitchContinuityParams {
  activeProfile: string
  descriptorConnectionId: null | string
  descriptorProfile: null | string
  locationPathname: string
  navigate: (to: string, options?: { replace?: boolean }) => void
  profileReady: boolean
  refreshSessions: (shouldPublish?: () => boolean) => Promise<boolean>
}

const normalizeConnectionId = (value: null | string | undefined): null | string => value?.trim() || null
const normalizeProfile = (value: string): string => value.trim() || 'default'

function profileSwitchPaneFocus(groupId: null | string, tree: LayoutNode | null): ProfileSwitchPaneFocus {
  const paneId =
    groupId !== null
      ? tree
        ? (findGroup(tree, groupId)?.active ?? null)
        : null
      : tree
        ? (findGroupOfPane(tree, 'workspace')?.active ?? null)
        : 'workspace'

  return {
    groupId,
    paneId
  }
}

interface ProfileSwitchOwnerContext {
  activation: ProfileSwitchActivation
  requestedConnectionId: null | string
}

function ownerMatchesSettledActivation(
  owner: SessionOwnerScope,
  { activation, requestedConnectionId }: ProfileSwitchOwnerContext
): boolean {
  const profile = normalizeProfile(activation.profile)
  const liveGatewayConnectionId = normalizeConnectionId(activation.liveGatewayConnectionId)
  const descriptorConnectionId = normalizeConnectionId(activation.descriptorConnectionId)
  const descriptorProfile = activation.descriptorProfile?.trim() || null
  const descriptorProfileMismatch =
    (descriptorConnectionId !== null || descriptorProfile !== null) &&
    normalizeProfile(descriptorProfile ?? '') !== profile
  const liveDescriptorMismatch = liveGatewayConnectionId !== null && liveGatewayConnectionId !== descriptorConnectionId

  if (descriptorProfileMismatch || liveDescriptorMismatch) {
    return false
  }

  if (isSessionOwnerRoute(owner)) {
    const exactConnectionId =
      normalizeConnectionId(requestedConnectionId) ?? liveGatewayConnectionId ?? descriptorConnectionId

    return (
      exactConnectionId !== null &&
      owner.connectionId.trim() === exactConnectionId &&
      normalizeProfile(owner.profile) === profile
    )
  }

  // A bare profile names the profile-keyed pool socket and is valid only when
  // this intent used the profile-only door. An explicit registry request must
  // always carry an exact owner route.
  return (
    normalizeConnectionId(requestedConnectionId) === null &&
    typeof owner === 'string' &&
    normalizeProfile(owner) === profile
  )
}

function currentActivation(
  descriptorConnectionId: null | string,
  descriptorProfile: null | string,
  activeProfile: string
): ProfileSwitchActivation {
  return {
    activationEpoch: gatewayActivationEpoch(),
    descriptorConnectionId: normalizeConnectionId(descriptorConnectionId),
    descriptorProfile: descriptorProfile?.trim() || null,
    liveGatewayConnectionId: normalizeConnectionId(activeGatewayConnectionId()),
    profile: normalizeProfile(activeProfile)
  }
}

/** Restore the last natively focused session after a live profile activation.
 * The fresh-draft barrier, activation epoch, descriptor/live publication and
 * guarded list refresh must all still belong to the same user intent. */
export function useProfileSwitchContinuity({
  activeProfile,
  descriptorConnectionId,
  descriptorProfile,
  locationPathname,
  navigate,
  profileReady,
  refreshSessions
}: ProfileSwitchContinuityParams): void {
  const profileSwitchBehavior = useStore($profileSwitchBehavior)
  const profileSwitchRestoreToken = useStore($profileSwitchRestoreToken)
  const freshSessionRequest = useStore($freshSessionRequest)
  const pendingConnectionId = useStore($pendingConnectionId)
  const focusedStoredSessionId = useStore($focusedStoredSessionId)
  const activeTreeGroup = useStore($activeTreeGroup)
  const layoutTree = useStore($layoutTree)
  const paneFocus = profileSwitchPaneFocus(activeTreeGroup, layoutTree)
  const currentNavigationToken = profileSwitchNavigationToken({
    focusedStoredSessionId,
    paneFocus,
    pathname: locationPathname
  })
  const anchorRef = useRef({
    activation: currentActivation(descriptorConnectionId, descriptorProfile, activeProfile),
    focusedStoredSessionId,
    paneFocus,
    pathname: locationPathname,
    requestedConnectionId: profilePickConnectionId()
  })

  anchorRef.current = {
    activation: currentActivation(descriptorConnectionId, descriptorProfile, activeProfile),
    focusedStoredSessionId,
    paneFocus,
    pathname: locationPathname,
    requestedConnectionId: profilePickConnectionId()
  }

  const normalMainWindow = !isHudWindow() && !isSecondaryWindow() && !isBrowserWindow()

  // Native pane activation does not change the router. Remember the focused
  // stored id under the exact settled source/profile scope.
  useEffect(() => {
    if (!normalMainWindow || !profileReady || !focusedStoredSessionId) {
      return
    }

    const activation = currentActivation(descriptorConnectionId, descriptorProfile, activeProfile)
    const requestedConnectionId = profilePickConnectionId()

    if (
      ownerMatchesSettledActivation(knownOwnerForSession(focusedStoredSessionId), {
        activation,
        requestedConnectionId
      })
    ) {
      setRememberedSessionId(focusedStoredSessionId, activeProfile)
    }
  }, [activeProfile, descriptorConnectionId, descriptorProfile, focusedStoredSessionId, normalMainWindow, profileReady])

  // `selectProfile` crosses the fresh-draft barrier synchronously. Give it a
  // synchronous read of the latest native focus so React cannot replace the
  // departing profile's anchor before the request captures it.
  useEffect(() => {
    if (!normalMainWindow) {
      return
    }

    return registerProfileSwitchAnchorCapture(() => {
      const anchor = anchorRef.current
      const focused = anchor.focusedStoredSessionId
      const requestedConnectionId = anchor.requestedConnectionId

      if (
        focused &&
        ownerMatchesSettledActivation(knownOwnerForSession(focused), {
          activation: anchor.activation,
          requestedConnectionId
        })
      ) {
        setRememberedSessionId(focused, anchor.activation.profile)
      }

      return { focusedStoredSessionId: focused, paneFocus: anchor.paneFocus, pathname: anchor.pathname }
    })
  }, [normalMainWindow])

  // The native fresh-request sequence authorizes exactly one source ->
  // null-selection -> fresh-route transition. A later New Chat supersedes it
  // even if the pathname/focus pair is unchanged.
  useEffect(() => {
    const token = profileSwitchRestoreToken

    if (!token) {
      return
    }

    if (!normalMainWindow) {
      clearProfileSwitchRestore(token.generation)

      return
    }

    observeProfileSwitchRestoreNavigation(
      token.generation,
      { focusedStoredSessionId, paneFocus, pathname: locationPathname },
      freshSessionRequest
    )
  }, [currentNavigationToken, freshSessionRequest, normalMainWindow, profileSwitchRestoreToken])

  // Restore only after current-main's exact activation has published and its
  // guarded session refresh has committed. Native focus/open APIs preserve the
  // profile-keyed tile placement and avoid duplicate tabs.
  useEffect(() => {
    const token = profileSwitchRestoreToken

    if (!token) {
      return
    }

    if (!normalMainWindow || profileSwitchBehavior !== 'restore_last_session' || pendingConnectionId) {
      clearProfileSwitchRestore(token.generation)

      return
    }

    if (token.draftNavigationToken === undefined || token.activation === undefined || !profileReady) {
      return
    }

    const boundActivation = token.activation
    const activation = currentActivation(descriptorConnectionId, descriptorProfile, activeProfile)
    const activationMatches =
      activation.activationEpoch === boundActivation.activationEpoch &&
      activation.descriptorConnectionId === boundActivation.descriptorConnectionId &&
      activation.descriptorProfile === boundActivation.descriptorProfile &&
      activation.liveGatewayConnectionId === boundActivation.liveGatewayConnectionId &&
      activation.profile === boundActivation.profile

    if (
      !activationMatches ||
      freshSessionRequest !== token.freshSessionRequestSequence ||
      currentNavigationToken !== token.draftNavigationToken
    ) {
      clearProfileSwitchRestore(token.generation)

      return
    }

    const exactActivation = () => {
      const current = $profileSwitchRestoreToken.get()
      const settled = currentActivation(descriptorConnectionId, descriptorProfile, activeProfile)
      const liveNavigationToken = profileSwitchNavigationToken({
        focusedStoredSessionId: $focusedStoredSessionId.get(),
        paneFocus: profileSwitchPaneFocus($activeTreeGroup.get(), $layoutTree.get()),
        pathname: locationPathname
      })

      return (
        current?.generation === token.generation &&
        current.draftNavigationToken === token.draftNavigationToken &&
        $freshSessionRequest.get() === token.freshSessionRequestSequence &&
        liveNavigationToken === token.draftNavigationToken &&
        settled.activationEpoch === boundActivation.activationEpoch &&
        settled.descriptorConnectionId === boundActivation.descriptorConnectionId &&
        settled.descriptorProfile === boundActivation.descriptorProfile &&
        settled.liveGatewayConnectionId === boundActivation.liveGatewayConnectionId &&
        settled.profile === boundActivation.profile
      )
    }

    if (!exactActivation()) {
      clearProfileSwitchRestore(token.generation)

      return
    }

    let cancelled = false

    void (async () => {
      const committed = await refreshSessions(() => !cancelled && exactActivation())

      if (cancelled || committed !== true || !exactActivation()) {
        clearProfileSwitchRestore(token.generation)

        return
      }

      const remembered = getRememberedSessionId(boundActivation.profile)
      const owner = remembered ? knownOwnerForSession(remembered) : undefined

      if (
        !remembered ||
        !ownerMatchesSettledActivation(owner, {
          activation: boundActivation,
          requestedConnectionId: token.requestedConnectionId
        })
      ) {
        clearProfileSwitchRestore(token.generation)

        return
      }

      clearProfileSwitchRestore(token.generation)

      if (focusOpenSession(remembered)) {
        return
      }

      markSelectionRestore()

      if (isSessionOwnerRoute(owner)) {
        requestSessionResume(remembered, owner)
      }

      openSession(remembered, navigate, 'in-place')
    })().catch(() => {
      if (!cancelled) {
        clearProfileSwitchRestore(token.generation)
      }
    })

    return () => {
      cancelled = true
    }
  }, [
    activeProfile,
    currentNavigationToken,
    descriptorConnectionId,
    descriptorProfile,
    freshSessionRequest,
    navigate,
    normalMainWindow,
    pendingConnectionId,
    profileReady,
    profileSwitchBehavior,
    profileSwitchRestoreToken,
    refreshSessions
  ])
}
