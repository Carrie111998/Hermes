import { atom } from 'nanostores'

import { readJson, writeJson } from '@/lib/storage'
import { normalizeProfileKey } from '@/store/profile'

/**
 * Device-scoped multi-pet visibility roster.
 *
 * Backend truth stays per-profile: each profile owns its own `display.pet` in
 * its `config.yaml`. The desktop only persists WHICH profiles' pets to show and
 * in which mode, scoped to this device (localStorage). Two modes:
 *
 * - `follow-active` (default): the visible pet follows `$activeGatewayProfile`
 *   — the current single-pet behavior, zero change. `entries` is ignored.
 * - `pinned` (opt-in): `entries` names the profiles whose pets render
 *   simultaneously.
 *
 * The roster invariant (enabled-profile cap) is enforced on EVERY load and
 * through every mutation API, so a persisted/hand-edited roster can never exceed
 * the cap and UI + non-UI writers agree.
 */

export type PetRosterMode = 'follow-active' | 'pinned'

export interface RosterEntry {
  profile: string
  enabled: boolean
  /** True only after a successfully loaded profile catalog confirms it is
   *  missing (deleted/renamed). Such rows stay visible as disabled with a
   *  "not found" affordance; they never count toward the enabled cap. */
  unavailable?: boolean
}

export interface StoredPetRoster {
  initialized: boolean
  mode: PetRosterMode
  /** Only meaningful when `mode === 'pinned'`. */
  entries: RosterEntry[]
}

/** Soft cap: a cost warning surfaces at this many enabled pinned profiles
 *  (each holds a leased socket + a polling lane). */
export const PINNED_SOFT_CAP = 4
/** Hard cap: no more than this many profiles may be enabled at once. Enforced
 *  deterministically at load and by every mutation API. */
export const PINNED_HARD_CAP = 8

const ROSTER_STORAGE_KEY = 'hermes.desktop.petRoster.v1'

function defaultRoster(): StoredPetRoster {
  return { initialized: true, mode: 'follow-active', entries: [] }
}

function normalizeEntry(value: unknown): RosterEntry | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const raw = value as Record<string, unknown>

  if (typeof raw.profile !== 'string' || !raw.profile.trim()) {
    return null
  }

  const entry: RosterEntry = {
    enabled: raw.enabled === true,
    profile: normalizeProfileKey(raw.profile)
  }

  if (raw.unavailable === true) {
    entry.unavailable = true
  }

  return entry
}

export interface RosterNormalization {
  /** Profiles that were enabled in the input but force-disabled by the cap. */
  clampedProfiles: string[]
  roster: StoredPetRoster
}

/**
 * Validate + default a raw stored value into a well-formed roster, clamping the
 * enabled set to `PINNED_HARD_CAP`. Persisted entry order is the deterministic
 * tie-breaker: the first eight enabled entries (in stored order) stay enabled,
 * the rest are disabled. Unavailable/disabled rows never count toward the cap.
 *
 * An `initialized: false` value is a first-boot marker and migrates to the
 * default (follow-active, empty) roster.
 */
export function normalizeStoredPetRosterWithClamp(input: unknown): RosterNormalization {
  if (!input || typeof input !== 'object') {
    return { clampedProfiles: [], roster: defaultRoster() }
  }

  const raw = input as Record<string, unknown>

  // First-boot migration: an explicit false resets to the default roster.
  if (raw.initialized === false) {
    return { clampedProfiles: [], roster: defaultRoster() }
  }

  const mode: PetRosterMode = raw.mode === 'pinned' ? 'pinned' : 'follow-active'
  const rawEntries = Array.isArray(raw.entries) ? raw.entries : []

  const parsed: RosterEntry[] = []

  for (const value of rawEntries) {
    const entry = normalizeEntry(value)

    if (entry) {
      parsed.push(entry)
    }
  }

  // Clamp regardless of mode: covers older builds, hand-edited storage, and a
  // follow-active roster later switched to pinned. Unavailable and disabled rows
  // never count toward the cap.
  let enabledKept = 0
  const clampedProfiles: string[] = []

  const entries = parsed.map(entry => {
    if (!entry.enabled || entry.unavailable) {
      return entry
    }

    enabledKept += 1

    if (enabledKept <= PINNED_HARD_CAP) {
      return entry
    }

    clampedProfiles.push(entry.profile)

    return { ...entry, enabled: false }
  })

  return {
    clampedProfiles,
    roster: { entries, initialized: true, mode }
  }
}

/** Spec-shaped pure normalizer (drops the clamp report). */
export function normalizeStoredPetRoster(input: unknown): StoredPetRoster {
  return normalizeStoredPetRosterWithClamp(input).roster
}

// A load-time clamp is recorded here and surfaced once the application wiring is
// ready (i18n + notifications available). The normalizer must not reach into the
// notification store during module initialization.
let pendingCapWarning: string[] = []

function loadRoster(): StoredPetRoster {
  const { clampedProfiles, roster } = normalizeStoredPetRosterWithClamp(readJson<unknown>(ROSTER_STORAGE_KEY))

  // Persist the normalized value so older builds / hand-edited storage converge
  // on the clamped invariant.
  writeJson(ROSTER_STORAGE_KEY, roster)

  if (clampedProfiles.length > 0) {
    pendingCapWarning = clampedProfiles
  }

  return roster
}

export const $petRoster = atom<StoredPetRoster>(loadRoster())

/** Drain the one-shot "roster was clamped to the cap" report. Returns the
 *  profiles that were force-disabled; empty when nothing was clamped. Called by
 *  the application wiring once translations + notifications are available. */
export function takeRosterCapWarning(): string[] {
  const out = pendingCapWarning
  pendingCapWarning = []

  return out
}

/** All roster writes flow through here so the invariant (shape + cap) holds no
 *  matter the caller. */
function commit(roster: StoredPetRoster): void {
  const normalized = normalizeStoredPetRoster(roster)
  $petRoster.set(normalized)
  writeJson(ROSTER_STORAGE_KEY, normalized)
}

export function setPetRosterMode(mode: PetRosterMode): void {
  commit({ ...$petRoster.get(), initialized: true, mode })
}

export function setPetRosterEntries(entries: RosterEntry[]): void {
  commit({ ...$petRoster.get(), entries, initialized: true })
}

/** Count enabled entries excluding unavailable rows (which never count). */
function enabledCount(roster: StoredPetRoster): number {
  return roster.entries.filter(entry => entry.enabled && !entry.unavailable).length
}

/**
 * Enable/disable a profile's pet in the pinned roster. Returns `false` (and
 * leaves the roster unchanged) when enabling would exceed `PINNED_HARD_CAP`;
 * the caller surfaces the translated cap explanation. Disabling always succeeds.
 */
export function setProfilePetEnabled(profile: string, enabled: boolean): boolean {
  const key = normalizeProfileKey(profile)
  const roster = $petRoster.get()
  const existing = roster.entries.find(entry => entry.profile === key)

  if (enabled && !existing?.enabled && !existing?.unavailable && enabledCount(roster) >= PINNED_HARD_CAP) {
    return false
  }

  const entries = existing
    ? roster.entries.map(entry => (entry.profile === key ? { ...entry, enabled, unavailable: false } : entry))
    : [...roster.entries, { enabled, profile: key }]

  commit({ ...roster, entries, initialized: true })

  return true
}

/** Whether a profile is currently shown in pinned mode (enabled + available). */
export function isProfilePetEnabled(profile: string): boolean {
  const key = normalizeProfileKey(profile)

  return $petRoster.get().entries.some(entry => entry.profile === key && entry.enabled && !entry.unavailable)
}
