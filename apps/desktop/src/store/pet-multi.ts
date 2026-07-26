import { atom } from 'nanostores'

import type { ClientSessionState } from '@/app/types'
import { createClientSessionState } from '@/lib/chat-runtime'
import { normalizeProfileKey } from '@/store/profile'

import type { PetActivity, PetInfo } from './pet'

/**
 * Per-profile pet state — the multi-pet foundation.
 *
 * One slice per profile, keyed by normalized profile name. Activity is
 * SESSION-DERIVED: steady signals (busy / reasoning / running tools / awaiting
 * input) are tracked per `(profile, runtimeSessionId)` in `sessionActivity` and
 * aggregated by `deriveProfileActivity`; transient reaction beats
 * (celebrate / error / justCompleted) live per profile with a generation-counter
 * timer so a stale beat can't clear a newer one.
 *
 * `$profilePets` is copy-on-write: every reducer publishes a fresh Map so
 * computeds (and the active-profile backwards-compat atoms in `pet.ts`) re-fire.
 */

export interface SessionActivity {
  busy: boolean
  reasoning: boolean
  awaitingInput: boolean
  /** Overlapping tool calls — a set, not a boolean, so two concurrent tools keep
   *  toolRunning true until BOTH complete. */
  activeToolIds: Set<string>
  /** Durable session id learned from session.info/resume; lets a stored-id busy
   *  lookup survive runtime-id rotation. */
  storedSessionId?: string
}

export interface ProfileBeat {
  celebrate: boolean
  error: boolean
  justCompleted: boolean
  /** Generation counter for timer safety — an old timer can't clear a newer beat. */
  gen: number
}

export interface ProfilePetState {
  info: PetInfo
  activity: PetActivity
  replyText: string | null
  unread: boolean
  /** Runtime session id that produced the unread reply (used for submit). */
  sourceSessionId?: string | null
  /** Durable session id for navigation (survives compression/rehoming). */
  sourceDurableSessionId?: string | null
}

function defaultProfilePetState(): ProfilePetState {
  return {
    activity: {},
    info: { enabled: false },
    replyText: null,
    unread: false
  }
}

export const $profilePets = atom<ReadonlyMap<string, ProfilePetState>>(new Map())

// Per-session steady state, keyed `${profile}::${runtimeSessionId}`. Module-local
// (not an atom): reactivity flows through the copy-on-write `$profilePets` bump
// each reducer publishes.
const sessionActivity = new Map<string, SessionActivity>()
const beats = new Map<string, ProfileBeat>()
// Global "a session is awaiting the user" sync (use-pet-bridge) — OR-ed into the
// derived awaitingInput so it survives a session-reducer republish.
const manualAwaitingInput = new Map<string, boolean>()
// Background (profile-targeted) submit state. A background submit drives the
// profile's busy pose through these profile-scoped flags — never the foreground
// $busy — so a reply sent to a non-active profile from its overlay still animates
// that profile's pet.
const profileSubmitBusy = new Map<string, boolean>()
const profileAwaitingResponse = new Map<string, boolean>()
// Per-(profile, runtimeId) background session-state cache. The submit pipeline's
// optimistic inserts run against this so a background submit never publishes into
// the foreground session cache or $messages.
const backgroundSessionStates = new Map<string, ClientSessionState>()

export function activityKey(profile: string, runtimeSessionId: string): string {
  return `${normalizeProfileKey(profile)}::${runtimeSessionId}`
}

function ensureSession(profile: string, runtimeSessionId: string): SessionActivity {
  const key = activityKey(profile, runtimeSessionId)
  let activity = sessionActivity.get(key)

  if (!activity) {
    activity = { activeToolIds: new Set(), awaitingInput: false, busy: false, reasoning: false }
    sessionActivity.set(key, activity)
  }

  return activity
}

/** Aggregate a profile's sessions + beats into coarse activity signals. */
export function deriveProfileActivity(profile: string): PetActivity {
  const key = normalizeProfileKey(profile)
  const prefix = `${key}::`
  const sessions: SessionActivity[] = []

  for (const [sessionKey, activity] of sessionActivity) {
    if (sessionKey.startsWith(prefix)) {
      sessions.push(activity)
    }
  }

  const beat = beats.get(key)

  return {
    awaitingInput: sessions.some(s => s.awaitingInput) || (manualAwaitingInput.get(key) ?? false),
    busy:
      sessions.some(s => s.busy) ||
      (profileSubmitBusy.get(key) ?? false) ||
      (profileAwaitingResponse.get(key) ?? false),
    celebrate: beat?.celebrate ?? false,
    error: beat?.error ?? false,
    justCompleted: beat?.justCompleted ?? false,
    reasoning: sessions.some(s => s.reasoning),
    toolRunning: sessions.some(s => s.activeToolIds.size > 0)
  }
}

/** Copy-on-write publish of one profile's slice. */
function updateProfilePet(profile: string, updater: (prev: ProfilePetState) => ProfilePetState): void {
  const key = normalizeProfileKey(profile)
  const prev = $profilePets.get()
  const entry = prev.get(key) ?? defaultProfilePetState()
  const next = new Map(prev)
  next.set(key, updater(entry))
  $profilePets.set(next)
}

/** Recompute a profile's activity from its sessions/beats and republish.
 *  Preserves a manually-synced awaitingInput unless a transition forces it. */
function publishProfileActivity(profile: string, forceAwaitingInput?: boolean): void {
  const derived = deriveProfileActivity(profile)

  updateProfilePet(profile, prev => ({
    ...prev,
    activity: {
      ...derived,
      awaitingInput: forceAwaitingInput ?? (derived.awaitingInput || (prev.activity.awaitingInput ?? false))
    }
  }))
}

export function ensureProfilePet(profile: string): void {
  const key = normalizeProfileKey(profile)

  if (!$profilePets.get().has(key)) {
    updateProfilePet(key, prev => prev)
  }
}

export function getProfilePet(profile: string): ProfilePetState | undefined {
  return $profilePets.get().get(normalizeProfileKey(profile))
}

// ── Session-derived reducers ───────────────────────────────────────────────

export function setSessionBusy(profile: string, runtimeSessionId: string, busy: boolean): void {
  ensureSession(profile, runtimeSessionId).busy = busy
  publishProfileActivity(profile)
}

export function setSessionReasoning(profile: string, runtimeSessionId: string, reasoning: boolean): void {
  ensureSession(profile, runtimeSessionId).reasoning = reasoning
  publishProfileActivity(profile)
}

export function setSessionAwaitingInput(profile: string, runtimeSessionId: string, awaiting: boolean): void {
  ensureSession(profile, runtimeSessionId).awaitingInput = awaiting
  publishProfileActivity(profile)
}

/** Add one running tool id (empty/missing ids are ignored). */
export function startTool(profile: string, runtimeSessionId: string, toolId: string | null | undefined): void {
  if (!toolId) {
    return
  }

  ensureSession(profile, runtimeSessionId).activeToolIds.add(toolId)
  publishProfileActivity(profile)
}

/** Remove ONLY this tool id; overlapping tools keep toolRunning true. */
export function completeTool(profile: string, runtimeSessionId: string, toolId: string | null | undefined): void {
  if (!toolId) {
    return
  }

  const activity = sessionActivity.get(activityKey(profile, runtimeSessionId))

  if (!activity) {
    return
  }

  activity.activeToolIds.delete(toolId)
  publishProfileActivity(profile)
}

/** Bind a durable stored session id to a runtime session (session.info/resume). */
export function bindSessionStoredId(
  profile: string,
  runtimeSessionId: string,
  storedSessionId: string | null | undefined
): void {
  if (!storedSessionId) {
    return
  }

  ensureSession(profile, runtimeSessionId).storedSessionId = storedSessionId
}

/**
 * Terminal transition (message.complete / error / session.info running=false):
 * clear the session's steady state. `celebrate` fires a completion beat;
 * `error` fires an error beat. awaitingInput is cleared — a normal completion
 * returns to idle, not waiting (waiting means an unresolved clarify/approval).
 */
export function terminateSession(
  profile: string,
  runtimeSessionId: string,
  opts: { celebrate?: boolean; error?: boolean } = {}
): void {
  const key = activityKey(profile, runtimeSessionId)
  const activity = sessionActivity.get(key)

  if (activity) {
    activity.activeToolIds = new Set()
    activity.busy = false
    activity.reasoning = false
    activity.awaitingInput = false
  }

  if (opts.celebrate) {
    flashProfileBeat(profile, { celebrate: true }, 2200)
  } else if (opts.error) {
    flashProfileBeat(profile, { error: true })
  } else {
    publishProfileActivity(profile, false)
  }
}

/** Session deleted/removed: drop its activity entry entirely. */
export function removeSessionActivity(profile: string, runtimeSessionId: string): void {
  if (sessionActivity.delete(activityKey(profile, runtimeSessionId))) {
    publishProfileActivity(profile)
  }
}

/** Runtime-id replacement (rehomed/compression/resume): migrate the entry. */
export function replaceSessionRuntimeId(
  profile: string,
  oldRuntimeId: string,
  newRuntimeId: string
): void {
  const oldKey = activityKey(profile, oldRuntimeId)
  const activity = sessionActivity.get(oldKey)

  if (!activity || oldRuntimeId === newRuntimeId) {
    return
  }

  sessionActivity.delete(oldKey)
  sessionActivity.set(activityKey(profile, newRuntimeId), activity)
  publishProfileActivity(profile)
}

// ── Transient beats ────────────────────────────────────────────────────────

/** Fire a transient reaction beat that decays back to steady after `ms`. The
 *  generation counter guarantees a stale timer can't clear a newer beat. */
export function flashProfileBeat(
  profile: string,
  fields: Partial<Pick<ProfileBeat, 'celebrate' | 'error' | 'justCompleted'>>,
  ms = 1600
): void {
  const key = normalizeProfileKey(profile)
  const beat = beats.get(key) ?? { celebrate: false, error: false, gen: 0, justCompleted: false }
  const gen = beat.gen + 1

  // Clear siblings first so a stale one can't win the priority race (error
  // outranks celebrate in derivePetState).
  beats.set(key, { celebrate: false, error: false, gen, justCompleted: false, ...fields })
  publishProfileActivity(profile)

  setTimeout(() => {
    const current = beats.get(key)

    if (current && current.gen === gen) {
      beats.set(key, { celebrate: false, error: false, gen, justCompleted: false })
      publishProfileActivity(profile)
    }
  }, ms)
}

// ── Unread / reply / info (per profile) ────────────────────────────────────

const REPLY_TEXT_MAX = 200

export function markProfilePetUnread(profile: string, sourceSessionId?: string | null): void {
  updateProfilePet(profile, prev => ({
    ...prev,
    sourceSessionId: sourceSessionId ?? prev.sourceSessionId,
    unread: true
  }))
}

/** Clear unread for a specific source session only (focusing session A must not
 *  erase session B's unread within the same profile). */
export function clearProfilePetUnread(profile: string, sourceSessionId?: string | null): void {
  updateProfilePet(profile, prev => {
    if (sourceSessionId != null && prev.sourceSessionId != null && prev.sourceSessionId !== sourceSessionId) {
      return prev
    }

    return { ...prev, unread: false }
  })
}

export function setProfilePetReplyText(profile: string, text: string, sourceSessionId?: string | null): void {
  const trimmed = text.trim()

  if (!trimmed || /^\[SILENT\]/i.test(trimmed)) {
    return
  }

  const capped = trimmed.length > REPLY_TEXT_MAX ? trimmed.slice(0, REPLY_TEXT_MAX) : trimmed

  updateProfilePet(profile, prev => ({
    ...prev,
    replyText: capped,
    sourceSessionId: sourceSessionId ?? prev.sourceSessionId
  }))
}

export function clearProfilePetReplyText(profile: string, sourceSessionId?: string | null): void {
  updateProfilePet(profile, prev => {
    if (sourceSessionId != null && prev.sourceSessionId != null && prev.sourceSessionId !== sourceSessionId) {
      return prev
    }

    return { ...prev, replyText: null }
  })
}

export function setProfilePetInfo(profile: string, info: PetInfo): void {
  updateProfilePet(profile, prev => ({ ...prev, info }))
}

/** Profile-level "awaiting the user" sync (use-pet-bridge's global mirror). */
export function setProfileManualAwaitingInput(profile: string, awaiting: boolean): void {
  const key = normalizeProfileKey(profile)
  manualAwaitingInput.set(key, awaiting)
  publishProfileActivity(profile)
}

/** Direct activity merge for a profile (legacy setPetActivity). */
export function mergeProfileActivity(profile: string, next: Partial<PetActivity>): void {
  updateProfilePet(profile, prev => ({ ...prev, activity: { ...prev.activity, ...next } }))
}

/** Wholesale-replace a profile's activity (the overlay pushes a full snapshot
 *  and expects it to replace, not merge over stale fields). */
export function replaceProfileActivity(profile: string, activity: PetActivity): void {
  updateProfilePet(profile, prev => ({ ...prev, activity }))
}

// ── Registry queries (durable-id resolution) ───────────────────────────────

/** Whether any session under `profile` bound to `storedSessionId` is busy. */
export function hasBusyActivityForStoredSession(profile: string, storedSessionId: string): boolean {
  const prefix = `${normalizeProfileKey(profile)}::`

  for (const [key, activity] of sessionActivity) {
    if (key.startsWith(prefix) && activity.storedSessionId === storedSessionId && activity.busy) {
      return true
    }
  }

  return false
}

/** Whether a specific runtime session under `profile` is currently busy. */
export function profileRuntimeBusy(profile: string, runtimeSessionId: string): boolean {
  return sessionActivity.get(activityKey(profile, runtimeSessionId))?.busy ?? false
}

/** The one runtime id bound to `storedSessionId` under `profile`, or null for
 *  zero/multiple matches (callers then use the durable session.resume path). */
export function runtimeIdForStoredActivity(profile: string, storedSessionId: string): null | string {
  const prefix = `${normalizeProfileKey(profile)}::`
  let found: null | string = null

  for (const [key, activity] of sessionActivity) {
    if (key.startsWith(prefix) && activity.storedSessionId === storedSessionId) {
      const runtimeId = key.slice(prefix.length)

      if (found !== null) {
        return null // ambiguous
      }

      found = runtimeId
    }
  }

  return found
}

// ── Layer 6c (profile-safe submission) ─────────────────────────────────────

/**
 * Composite-keyed background session-state adapter. The submit pipeline's
 * optimistic inserts/rewrites/drops run against a per-(profile, runtimeId) cache
 * here, so a background submit can keep its own bookkeeping without ever
 * publishing into the foreground session cache or `$messages`. Returns the
 * updated state (the pipeline calls this for its side effect).
 */
export function updateBackgroundSessionState(
  profile: string,
  runtimeId: string,
  updater: (state: ClientSessionState) => ClientSessionState,
  storedId?: string | null
): ClientSessionState {
  const key = `${normalizeProfileKey(profile)}::${runtimeId}`
  const prev = backgroundSessionStates.get(key) ?? createClientSessionState(storedId ?? null)
  const next = updater(prev)
  backgroundSessionStates.set(key, next)

  return next
}

/** Profile-scoped awaiting-response flag for background submits — drives the
 *  profile's busy pose, never the foreground `$awaitingResponse`. */
export function setProfileAwaitingResponse(profile: string, awaiting: boolean): void {
  const key = normalizeProfileKey(profile)
  profileAwaitingResponse.set(key, awaiting)
  publishProfileActivity(profile)
}

/** Profile-scoped submit-busy flag for background submits — drives the profile's
 *  busy pose, never the foreground `$busy`. */
export function setProfileSubmitBusy(profile: string, busy: boolean): void {
  const key = normalizeProfileKey(profile)
  profileSubmitBusy.set(key, busy)
  publishProfileActivity(profile)
}

/** Test-only: clear all per-session activity, beats, and profile slices so cases
 *  don't leak into one another (the registries are module-global). */
export function __resetPetMultiForTests(): void {
  sessionActivity.clear()
  beats.clear()
  manualAwaitingInput.clear()
  profileSubmitBusy.clear()
  profileAwaitingResponse.clear()
  backgroundSessionStates.clear()
  $profilePets.set(new Map())
}
