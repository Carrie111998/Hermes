// Parse and serialize the opaque target-id strings that the renderer passes
// to the main process (e.g. via openInstance(targetId)) and that main validates
// before binding a WindowTarget.
//
// The id is a closed, single-line string with one of three shapes:
//
//   primary
//   configured-profile:<valid-profile-name>
//   forced-local-profile:<valid-profile-name>
//
// There is no URL, token, or raw backend descriptor in an id — those are
// connection/transport concerns, never target identity. The id is the only
// renderer-reachable handle for a target; the BackendTarget model lives in
// main and is never sent across the bridge.
//
// parseTargetId() validates at every layer:
//   - the kind prefix is one of the three closed kinds (else "unknown target kind").
//   - profile-carrying kinds have exactly one name segment after the colon
//     (else "malformed").
//   - the name passes the shared profile-name validator, which rejects
//     reserved names, invalid characters, and oversize names.
//
// serializeTargetId() is the inverse: a BackendTarget -> its id string. It is
// the same string canonicalTargetKey() produces, so equivalent targets
// serialize to the same id.

import { type BackendTarget, canonicalTargetKey } from './backend-target'
import { isValidProfileName } from './profile-name'

/** The closed set of kind prefixes a target id may carry. */
const KNOWN_KINDS = new Set(['primary', 'configured-profile', 'forced-local-profile'])

/** Profile-carrying kinds — they require a `:<name>` segment. */
const PROFILE_KINDS = new Set(['configured-profile', 'forced-local-profile'])

/** A loose ceiling on the id string length so a runaway input fails fast. */
const MAX_TARGET_ID_LENGTH = 128

export type TargetIdParseResult =
  | { ok: true; target: BackendTarget }
  | { ok: false; reason: string }

/**
 * Validate an opaque target-id string and return the parsed BackendTarget, or
 * a non-throwing { ok: false, reason } so the IPC handler can return a
 * structured `ok: false, error` to the renderer without an exception crossing
 * the bridge.
 */
export function parseTargetId(id: string): TargetIdParseResult {
  if (typeof id !== 'string') {
    return { ok: false, reason: 'malformed: not a string' }
  }

  const trimmed = id.trim()

  if (!trimmed) {
    return { ok: false, reason: 'malformed: empty id' }
  }

  if (trimmed.length > MAX_TARGET_ID_LENGTH) {
    return { ok: false, reason: 'oversized: id exceeds maximum length' }
  }

  // `primary` is the only kind with no `:` segment.
  if (trimmed === 'primary') {
    return { ok: true, target: { kind: 'primary' } }
  }

  const colon = trimmed.indexOf(':')

  if (colon < 0) {
    return { ok: false, reason: `unknown target kind: ${trimmed}` }
  }

  const kind = trimmed.slice(0, colon)
  const rest = trimmed.slice(colon + 1)

  if (!KNOWN_KINDS.has(kind)) {
    return { ok: false, reason: `unknown target kind: ${kind}` }
  }

  if (!PROFILE_KINDS.has(kind)) {
    // `primary` was handled above; any other non-profile kind with a `:` is
    // malformed (e.g. "primary:extra").
    return { ok: false, reason: `malformed: unexpected segment for ${kind}` }
  }

  // A profile-carrying id has exactly one `:` — a second colon makes the shape
  // ambiguous and is rejected rather than silently truncated.
  if (rest.includes(':')) {
    return { ok: false, reason: `malformed: extra segment in id` }
  }

  if (!rest) {
    return { ok: false, reason: `malformed: missing profile name for ${kind}` }
  }

  if (!isValidProfileName(rest)) {
    // isValidProfileName rejects reserved names, invalid chars, and oversize
    // names. The reason distinguishes reserved from invalid so the caller can
    // surface a precise error.
    return { ok: false, reason: `invalid profile name: ${rest}` }
  }

  const profile = rest

  if (kind === 'configured-profile') {
    return { ok: true, target: { kind: 'configured-profile', profile } }
  }

  return { ok: true, target: { kind: 'forced-local-profile', profile } }
}

/**
 * Serialize a BackendTarget to its opaque id string. This is the inverse of
 * parseTargetId and the same string canonicalTargetKey() produces, so
 * equivalent targets serialize to the same id.
 */
export function serializeTargetId(target: BackendTarget): string {
  return canonicalTargetKey(target)
}