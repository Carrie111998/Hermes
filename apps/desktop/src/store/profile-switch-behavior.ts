import { atom } from 'nanostores'

import { type Codec, persistentAtom } from '@/lib/persisted'
import { storedString } from '@/lib/storage'

export type ProfileSwitchBehavior = 'fresh_draft' | 'restore_last_session'

export interface ProfileSwitchPaneFocus {
  groupId: null | string
  paneId: null | string
}

export interface ProfileSwitchNavigation {
  focusedStoredSessionId: null | string
  paneFocus: ProfileSwitchPaneFocus
  pathname: string
}

export interface ProfileSwitchActivation {
  activationEpoch: number
  descriptorConnectionId: null | string
  descriptorProfile: null | string
  liveGatewayConnectionId: null | string
  profile: string
}

export interface ProfileSwitchRestoreToken {
  activation?: ProfileSwitchActivation
  draftNavigationToken?: string
  freshSessionRequestSequence: number
  generation: number
  requestedConnectionId: null | string
  requestedProfile: string
  sourceNavigation?: ProfileSwitchNavigation
}

const STORAGE_KEY = 'hermes.desktop.profile-switch-behavior.v1'

function normalizeProfileSwitchBehavior(value: null | string): ProfileSwitchBehavior {
  return value === 'restore_last_session' ? value : 'fresh_draft'
}

const behaviorCodec: Codec<ProfileSwitchBehavior> = {
  decode: normalizeProfileSwitchBehavior,
  encode: value => value
}

export const $profileSwitchBehavior = persistentAtom<ProfileSwitchBehavior>(STORAGE_KEY, 'fresh_draft', behaviorCodec)

export const $profileSwitchRestoreToken = atom<ProfileSwitchRestoreToken | null>(null)

let switchGeneration = 0
let captureAnchor: null | (() => ProfileSwitchNavigation) = null

const normalizeConnectionId = (value: null | string | undefined): null | string => value?.trim() || null
const normalizeProfile = (value: string): string => value.trim() || 'default'
const isFreshDraftWorkspaceFocus = ({ groupId, paneId }: ProfileSwitchPaneFocus): boolean =>
  groupId === null && paneId === 'workspace'

export const profileSwitchNavigationToken = ({
  focusedStoredSessionId,
  paneFocus,
  pathname
}: ProfileSwitchNavigation): string =>
  JSON.stringify([pathname, focusedStoredSessionId, paneFocus.groupId, paneFocus.paneId])

export function getProfileSwitchBehavior(): ProfileSwitchBehavior {
  return $profileSwitchBehavior.get()
}

export function setProfileSwitchBehavior(value: ProfileSwitchBehavior): void {
  $profileSwitchBehavior.set(value)
}

/** Capture the departing profile's native focus, then replace every older
 * restore generation with one exact requested source/profile/fresh request. */
export function requestProfileSwitchRestore(
  requestedProfile: string,
  requestedConnectionId: null | string,
  freshSessionRequestSequence: number
): ProfileSwitchRestoreToken {
  const sourceNavigation = captureAnchor?.()

  const token = {
    freshSessionRequestSequence,
    generation: ++switchGeneration,
    requestedConnectionId: normalizeConnectionId(requestedConnectionId),
    requestedProfile: normalizeProfile(requestedProfile),
    ...(sourceNavigation === undefined ? {} : { sourceNavigation })
  }

  $profileSwitchRestoreToken.set(token)

  return token
}

/** A newer fresh draft or profile-switch intent supersedes every older
 * asynchronous restore. Old generations can never clear a newer token. */
export function supersedeProfileSwitchRestore(): number {
  const generation = ++switchGeneration
  $profileSwitchRestoreToken.set(null)

  return generation
}

/** Central fresh-draft cancellation seam. The profile switch's own native
 * request may consume its authorized sequence; every other draft is newer
 * user intent and supersedes the pending restore. */
export function supersedeProfileSwitchRestoreForFreshDraft(freshSessionRequestSequence?: number): void {
  const token = $profileSwitchRestoreToken.get()

  if (
    !token ||
    token.freshSessionRequestSequence === freshSessionRequestSequence ||
    (freshSessionRequestSequence !== undefined && freshSessionRequestSequence < token.freshSessionRequestSequence)
  ) {
    return
  }

  supersedeProfileSwitchRestore()
}

export function isCurrentProfileSwitchGeneration(generation: number): boolean {
  return generation === switchGeneration
}

/** Bind only the still-current request to the route that actually settled.
 * Requested registry identity, live gateway scope identity, and Electron's
 * committed descriptor identity are distinct domains and stay distinct. */
export function bindProfileSwitchRestore(generation: number, activation: ProfileSwitchActivation): boolean {
  const token = $profileSwitchRestoreToken.get()

  if (token?.generation !== generation) {
    return false
  }

  const requestedConnectionId = normalizeConnectionId(token.requestedConnectionId)
  const liveGatewayConnectionId = normalizeConnectionId(activation.liveGatewayConnectionId)
  const descriptorConnectionId = normalizeConnectionId(activation.descriptorConnectionId)
  const descriptorProfile = activation.descriptorProfile?.trim() || null
  const profile = normalizeProfile(activation.profile)

  const explicitSourceMismatch =
    requestedConnectionId !== null &&
    (liveGatewayConnectionId !== requestedConnectionId || descriptorConnectionId !== requestedConnectionId)

  // Profile-only local/override pool sockets are keyed by profile and publish
  // no live registry connection. Shared-primary routes publish the same source
  // in both domains. A non-null live source with a missing/different committed
  // descriptor is a torn publication and must fail closed.
  const profileDoorPublicationMismatch =
    requestedConnectionId === null &&
    liveGatewayConnectionId !== null &&
    liveGatewayConnectionId !== descriptorConnectionId
  const descriptorProfileMismatch =
    (descriptorConnectionId !== null || descriptorProfile !== null) &&
    normalizeProfile(descriptorProfile ?? '') !== profile

  if (
    explicitSourceMismatch ||
    profileDoorPublicationMismatch ||
    descriptorProfileMismatch ||
    token.requestedProfile !== profile
  ) {
    clearProfileSwitchRestore(generation)

    return false
  }

  $profileSwitchRestoreToken.set({
    ...token,
    activation: {
      activationEpoch: activation.activationEpoch,
      descriptorConnectionId,
      descriptorProfile,
      liveGatewayConnectionId,
      profile
    }
  })

  return true
}

/** Observe the one allowed source -> null-selection -> fresh-draft sequence.
 * The native fresh-request identity is authoritative: a later request cancels
 * even when pathname and focus happen to remain byte-identical. */
export function observeProfileSwitchRestoreNavigation(
  generation: number,
  navigation: ProfileSwitchNavigation,
  freshSessionRequestSequence: number
): boolean {
  const token = $profileSwitchRestoreToken.get()

  if (token?.generation !== generation) {
    return false
  }

  if (token.sourceNavigation === undefined || token.freshSessionRequestSequence !== freshSessionRequestSequence) {
    clearProfileSwitchRestore(generation)

    return false
  }

  const currentNavigationToken = profileSwitchNavigationToken(navigation)

  if (token.draftNavigationToken !== undefined) {
    if (token.draftNavigationToken !== currentNavigationToken) {
      clearProfileSwitchRestore(generation)

      return false
    }

    return true
  }

  if (
    navigation.pathname === '/' &&
    navigation.focusedStoredSessionId === null &&
    isFreshDraftWorkspaceFocus(navigation.paneFocus)
  ) {
    $profileSwitchRestoreToken.set({ ...token, draftNavigationToken: currentNavigationToken })

    return true
  }

  if (navigation.pathname === '/' && navigation.focusedStoredSessionId === null) {
    clearProfileSwitchRestore(generation)

    return false
  }

  const stillOnSource = currentNavigationToken === profileSwitchNavigationToken(token.sourceNavigation)
  const nativeIntermediate =
    navigation.pathname === token.sourceNavigation.pathname &&
    navigation.focusedStoredSessionId === null &&
    isFreshDraftWorkspaceFocus(navigation.paneFocus)

  if (!stillOnSource && !nativeIntermediate) {
    clearProfileSwitchRestore(generation)
  }

  return false
}

/** Register the main renderer's synchronous native-focus capture. */
export function registerProfileSwitchAnchorCapture(capture: () => ProfileSwitchNavigation): () => void {
  captureAnchor = capture

  return () => {
    if (captureAnchor === capture) {
      captureAnchor = null
    }
  }
}

export function clearProfileSwitchRestore(generation: number): void {
  if ($profileSwitchRestoreToken.get()?.generation === generation) {
    $profileSwitchRestoreToken.set(null)
  }
}

/** @internal Reset client-local preference and coordination state for tests. */
export function _resetProfileSwitchBehaviorForTests(): void {
  switchGeneration = 0
  captureAnchor = null
  $profileSwitchBehavior.set(
    typeof window === 'undefined' ? 'fresh_draft' : normalizeProfileSwitchBehavior(storedString(STORAGE_KEY))
  )
  $profileSwitchRestoreToken.set(null)
}
