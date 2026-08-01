import { atom, computed } from 'nanostores'

import { persistBoolean, persistString, storedBoolean, storedString } from '@/lib/storage'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $busy } from '@/store/session'

/**
 * Petdex mascot state for the desktop floating pet.
 *
 * The spritesheet payload comes from the gateway `pet.info` RPC (shared with
 * the TUI). The animation *state* is derived here from the same activity
 * signals the chat already tracks, mirroring the priority order documented in
 * `agent/pet/state.py` so the Python and TS surfaces never drift.
 */

export type PetState = 'idle' | 'wave' | 'run' | 'failed' | 'review' | 'jump' | 'waiting'

export interface PetInfo {
  enabled: boolean
  slug?: string
  displayName?: string
  mime?: string
  spritesheetBase64?: string
  // Stable sheet revision (`mtime_ns:size`) from the gateway; lets the desktop
  // skip full sprite payload refreshes when the active pet hasn't changed.
  spritesheetRevision?: string
  frameW?: number
  frameH?: number
  framesPerState?: number
  // Real (padding-trimmed) frame count per state row, from the engine. Lets the
  // canvas step only frames that exist instead of a fixed framesPerState, which
  // would animate into the transparent padding of ragged sheets (blank flash).
  framesByState?: Record<string, number>
  // Concrete Codex row counts (e.g. running-right may have 8 frames even though
  // the Hermes "run" activity state uses the in-place running row).
  framesByRow?: Record<string, number>
  loopMs?: number
  scale?: number
  stateRows?: string[]
}

interface CachedPetInfo {
  profile: string
  info: PetInfo
}

export interface PetActivity {
  busy?: boolean
  awaitingInput?: boolean
  error?: boolean
  ready?: boolean
  celebrate?: boolean
}

/**
 * Resolve the animation state from app-global session activity.
 *
 * Session priority mirrors Codex Desktop: needs input → failed → ready →
 * running → idle. A direct pet reaction (`celebrate`) remains a transient beat
 * below actionable states, so it cannot hide a session that needs attention.
 */
export function derivePetState(activity: PetActivity): PetState {
  if (activity.awaitingInput) {
    return 'waiting'
  }

  if (activity.error) {
    return 'failed'
  }

  if (activity.ready) {
    return 'review'
  }

  if (activity.celebrate) {
    return 'jump'
  }

  if (activity.busy) {
    return 'run'
  }

  return 'idle'
}

export interface PetSessionActivitySets {
  attentionSessionIds: readonly string[]
  failedSessionIds: readonly string[]
  unreadFinishedSessionIds: readonly string[]
  workingSessionIds: readonly string[]
}

/** Collapse every live conversation into the one activity the global pet shows. */
export function deriveSessionPetActivity(sets: PetSessionActivitySets): PetActivity {
  return {
    awaitingInput: sets.attentionSessionIds.length > 0,
    busy: sets.workingSessionIds.length > 0,
    error: sets.failedSessionIds.length > 0,
    ready: sets.unreadFinishedSessionIds.length > 0
  }
}

// The full spritesheet normally arrives only after the gateway is ready. Keep
// one profile-scoped snapshot so the mascot can paint during backend startup,
// then let the first live `pet.info` response reconcile it.
const PET_INFO_CACHE_KEY = 'hermes.desktop.pet-info-cache.v1'

export function petInfoFromCache(raw: null | string, profile: string): PetInfo {
  if (!raw) {
    return { enabled: false }
  }

  try {
    const cached = JSON.parse(raw) as Partial<CachedPetInfo>
    const info = cached.info

    if (
      cached.profile !== profile ||
      !info ||
      typeof info !== 'object' ||
      typeof info.enabled !== 'boolean' ||
      (info.enabled && (typeof info.spritesheetBase64 !== 'string' || !info.spritesheetBase64))
    ) {
      return { enabled: false }
    }

    return info
  } catch {
    return { enabled: false }
  }
}

function loadCachedPetInfo(): PetInfo {
  return petInfoFromCache(storedString(PET_INFO_CACHE_KEY), petProfile())
}

export const $petInfo = atom<PetInfo>(loadCachedPetInfo())
export const $petActivity = atom<PetActivity>({})

/** Pet installed + enabled with a loaded spritesheet (ready to show/react). */
export const $petActive = computed($petInfo, info => info.enabled && Boolean(info.spritesheetBase64))

/**
 * Profile the pet RPCs should resolve against. Pets are per-profile — the active
 * pet (`display.pet.*`) and the installed sprites live under each profile's
 * HERMES_HOME — so every pet RPC carries this. The gateway no-ops it for the
 * launch profile (own-profile backends already resolve it) and rebinds for any
 * other profile, which is what makes per-profile pets work in app-global remote
 * mode (one backend serving every profile).
 */
export function petProfile(): string {
  return normalizeProfileKey($activeGatewayProfile.get())
}

/**
 * Pet-local "you have a new message" flag, surfaced as the overlay's mail icon.
 * Deliberately not real unread tracking: it flips on when a turn finishes while
 * the app isn't focused, and off when the user opens the app via the mail icon
 * (or returns to the window). No persistence — it's a glance hint, not state.
 */
export const $petUnread = atom(false)
export const markPetUnread = () => $petUnread.set(true)
export const clearPetUnread = () => $petUnread.set(false)

/** Update the app-global activity mirrored to both in-window and overlay pets. */
export const setPetActivity = (next: Partial<PetActivity>) => $petActivity.set({ ...$petActivity.get(), ...next })

let flashTimer: ReturnType<typeof setTimeout> | undefined

/** Fire a transient direct-reaction beat that decays to session activity. */
export const flashPetActivity = (next: Partial<PetActivity>, ms = 1600) => {
  setPetActivity({ celebrate: false, ...next })
  clearTimeout(flashTimer)
  flashTimer = setTimeout(() => setPetActivity({ celebrate: false }), ms)
}

export const setPetInfo = (info: PetInfo) => $petInfo.set(info)

/** Apply a gateway-authoritative pet snapshot and retain it for the next boot. */
export const cachePetInfo = (info: PetInfo) => {
  setPetInfo(info)
  persistString(PET_INFO_CACHE_KEY, JSON.stringify({ info, profile: petProfile() } satisfies CachedPetInfo))
}

/**
 * Resolve the live activity state from the dedicated activity atom, falling back
 * to the always-present `$busy` chat signal so the pet reacts out of the box.
 *
 * The primary renderer resolves all sessions into `$petActivity`, then mirrors
 * that atom to the gateway-less pop-out overlay so both surfaces agree.
 */
function deriveLivePetState(activity: PetActivity, busy: boolean): PetState {
  const live = activity.busy ?? busy

  return derivePetState({
    busy: live,
    awaitingInput: activity.awaitingInput,
    error: activity.error,
    ready: activity.ready,
    celebrate: activity.celebrate
  })
}

/**
 * Opt-in: let the floating mascot wander on its current surface while idle —
 * inside Hermes or across the current display when popped out. Pure desktop-
 * client behavior, so it lives in localStorage per-device, not per-profile.
 */
const ROAM_KEY = 'hermes.desktop.pet-roam.v1'
export const $petRoam = atom<boolean>(storedBoolean(ROAM_KEY, false))

export const setPetRoam = (on: boolean) => {
  $petRoam.set(on)
  persistBoolean(ROAM_KEY, on)
}

/**
 * The pose the roam loop is currently driving: `run` while walking a surface,
 * `jump` while hopping/falling between surfaces, or `null` at rest. Surfaced
 * through `$petState` (below) so the canvas animates the wander without any prop
 * change or re-render — it already subscribes to `$petState`.
 */
export const $petMotion = atom<PetState | null>(null)

/**
 * Horizontal travel direction while roaming: -1 left, 1 right, 0 not walking.
 * The floating pet maps this to the directional run row + mirror, keeping the
 * wander loop free of sprite-row knowledge.
 */
export const $petRoamDir = atom<-1 | 0 | 1>(0)

/**
 * Whether the agent-driven state is at rest (plain `idle`). The roam loop gates
 * on this — never on `$petState` itself, which would feed back on its own
 * `$petMotion`-driven pose and stall the wander.
 */
export const $petAtRest = computed(
  [$petActivity, $busy],
  (activity, busy): boolean => deriveLivePetState(activity, busy) === 'idle'
)

/**
 * The live pet state. Activity always wins; only when the agent is at rest does
 * a roam pose (walking → `run`, hopping → `jump`) show through, so the wander
 * reads as deliberate movement.
 */
export const $petState = computed([$petActivity, $busy, $petMotion], (activity, busy, motion): PetState => {
  const base = deriveLivePetState(activity, busy)

  return base === 'idle' && motion ? motion : base
})
