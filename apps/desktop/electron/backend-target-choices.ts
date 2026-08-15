// Main-owned catalog of non-secret backend target choices exposed to the
// renderer via the listWindowBackendTargets IPC.
//
// The contract: listWindowBackendTargets returns ONLY {id, label, description,
// current} choices — no URLs, tokens, descriptors, or any secret-bearing
// field. The choices are:
//
//   - primary                              always present
//   - configured-profile:<name>            one per sanitized connection
//                                         profile with a remote-like mode
//   - forced-local-profile:<name>          one for the active primary profile
//                                         and one per configured profile
//
// `current` marks the choice the primary window currently resolves to. When
// the active primary profile is the default (or has no remote route), primary
// is current. When the active primary profile has a configured remote route,
// that configured-profile:<name> choice is current (the primary backend is
// shared via that remote host, scoped per-request).
//
// validateOpenInstanceTargetId() accepts only one of the currently-valid
// choice ids — anything else returns { ok: false, reason: 'invalid-target' }
// so the openInstance IPC can return { ok: false, error: 'invalid-target' }
// without an exception crossing the bridge.

import { modeIsRemoteLike } from './connection-config'
import { isValidProfileName } from './profile-name'
import { parseTargetId } from './target-id'

/** A single non-secret choice in the backend target catalog. */
export interface BackendTargetChoice {
  /** The opaque target id (one of the closed shapes). */
  id: string
  /** Human-readable label for the picker. */
  label: string
  /** Short description of what this target routes to. */
  description: string
  /** True when this is the target the primary window currently resolves to. */
  current: boolean
}

/** A sanitized entry in the connection profiles map. */
export interface SanitizedProfileEntry {
  mode?: string
  url?: string
  [key: string]: unknown
}

export interface BackendTargetChoicesInput {
  /** The profile name the primary backend runs (e.g. 'default', 'worker'). */
  activePrimaryProfile: string
  /** Opaque target id bound to the IPC sender. */
  currentTargetId: string
  /** Sanitized per-profile connection overrides (from readDesktopConnectionConfig). */
  configuredProfiles: Record<string, SanitizedProfileEntry>
  /** Main-owned deletion tombstone predicate. Revoked profiles are omitted. */
  isProfileRevoked?: (profile: string) => boolean
  /** Main-owned local profile existence predicate. Missing profiles are omitted. */
  isProfileAvailable?: (profile: string) => boolean
}

/**
 * Build the non-secret backend target choice catalog. Pure: reads only the
 * two injected inputs, returns a stable array with no duplicate ids and no
 * secret fields.
 */
export function buildBackendTargetChoices(input: BackendTargetChoicesInput): BackendTargetChoice[] {
  const activeProfile = typeof input.activePrimaryProfile === 'string' ? input.activePrimaryProfile.trim() : 'default'
  const profiles = input.configuredProfiles && typeof input.configuredProfiles === 'object' ? input.configuredProfiles : {}

  // Profiles that count as remote routes (configured-profile choices).
  const remoteProfiles: string[] = []
  // All profile names that appear in the configured map (for forced-local).
  const allProfileNames: string[] = []

  for (const [name, entry] of Object.entries(profiles)) {
    // Drop malformed names — the same gate sanitizeConnectionProfiles uses.
    if (name !== 'default' && !isValidProfileName(name)) {
      continue
    }

    if (input.isProfileRevoked?.(name) || input.isProfileAvailable?.(name) === false) {
      continue
    }

    allProfileNames.push(name)

    if (entry && typeof entry === 'object' && (modeIsRemoteLike(entry.mode) || entry.mode === 'ssh')) {
      remoteProfiles.push(name)
    }
  }

  const currentTargetId = typeof input.currentTargetId === 'string' ? input.currentTargetId : 'primary'

  const choices: BackendTargetChoice[] = []

  // 1. Primary — always first.
  choices.push({
    id: 'primary',
    label: 'Primary backend',
    description: 'The default window backend for this profile.',
    current: currentTargetId === 'primary'
  })

  // 2. Configured (remote) routes.
  for (const name of remoteProfiles.sort()) {
    choices.push({
      id: `configured-profile:${name}`,
      label: name === activeProfile ? `${name} (current)` : name,
      description: `Connect to the configured remote backend for profile "${name}".`,
      current: currentTargetId === `configured-profile:${name}`
    })
  }

  // 3. Forced-local for the active primary profile.
  //    The active primary is always eligible for forced-local (it can spawn
  //    a local process even when its route is remote).
  const forcedLocalNames = new Set<string>([activeProfile])

  // And for every configured profile (remote or local-mode — any saved profile
  // can be forced local).
  for (const name of allProfileNames.sort()) {
    forcedLocalNames.add(name)
  }

  for (const name of [...forcedLocalNames].sort()) {
    // Skip reserved/invalid names defensively (the configured map is already
    // gated, but the active primary could in theory be odd).
    if (name !== 'default' && !isValidProfileName(name)) {
      continue
    }

    if (input.isProfileRevoked?.(name) || input.isProfileAvailable?.(name) === false) {
      continue
    }

    choices.push({
      id: `forced-local-profile:${name}`,
      label: `${name} (local)`,
      description: `Spawn a local backend process for profile "${name}", bypassing remote resolution.`,
      current: currentTargetId === `forced-local-profile:${name}`
    })
  }

  return choices
}

export type OpenInstanceValidationResult =
  | { ok: true; id: string }
  | { ok: false; reason: string }

export type OpenInstanceRequest =
  | { ok: true; mode: 'inherit' }
  | { ok: true; mode: 'primary' }
  | { ok: true; mode: 'target'; id: string }
  | { ok: false; reason: 'invalid-target' }

/**
 * Validate that an id is one of the currently-valid backend target choices.
 * Returns { ok: true, id } for a valid id, or { ok: false, reason: 'invalid-target' }
 * for anything else — so the openInstance IPC returns a structured error
 * rather than throwing.
 */
export function validateOpenInstanceTargetId(
  id: string,
  choices: BackendTargetChoice[]
): OpenInstanceValidationResult {
  if (typeof id !== 'string' || !id.trim()) {
    return { ok: false, reason: 'invalid-target' }
  }

  const trimmed = id.trim()

  if (!parseTargetId(trimmed).ok) {
    return { ok: false, reason: 'invalid-target' }
  }

  // The id must be one of the currently-valid choice ids. This is an exact
  // membership check against the live catalog, so a stale or revoked id
  // (e.g. a configured profile that was removed since the last
  // listWindowBackendTargets call) is rejected.
  for (const choice of choices) {
    if (choice.id === trimmed) {
      return { ok: true, id: trimmed }
    }
  }

  return { ok: false, reason: 'invalid-target' }
}

/**
 * Classify an ordinary inherited open separately from explicit target choices.
 * Only an omitted renderer argument inherits. `primary` remains a deliberate
 * override, and every opaque target still requires membership in the live
 * main-owned catalog.
 */
export function classifyOpenInstanceRequest(
  requestedTargetId: unknown,
  choices: BackendTargetChoice[]
): OpenInstanceRequest {
  if (requestedTargetId === undefined) {
    return { ok: true, mode: 'inherit' }
  }

  if (requestedTargetId === null || requestedTargetId === '' || requestedTargetId === 'primary') {
    return { ok: true, mode: 'primary' }
  }

  if (typeof requestedTargetId !== 'string') {
    return { ok: false, reason: 'invalid-target' }
  }

  const validation = validateOpenInstanceTargetId(requestedTargetId, choices)

  return validation.ok
    ? { ok: true, mode: 'target', id: validation.id }
    : { ok: false, reason: 'invalid-target' }
}