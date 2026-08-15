// Profile-name validation shared across the desktop main process.
//
// This mirrors hermes_cli.profiles.validate_profile_name() (and the
// _PROFILE_ID_RE / _RESERVED_NAMES it enforces) so the desktop never hands the
// backend a profile name its own resolver would reject and exit on. Keep this
// aligned with the Python source; the regex and reserved set must not drift.
//
// `default` is a special pass-through alias for the built-in root profile,
// matching the Python validator's explicit exemption.

/**
 * The raw profile-id regex: /^[a-z0-9][a-z0-9_-]{0,63}$/.
 *
 * Exported so the desktop main process (and any module that needs raw pattern
 * matching without the reserved-name / default-emission layers) imports one
 * shared authority instead of re-deriving a private copy. This mirrors
 * hermes_cli.profiles._PROFILE_ID_RE and must not drift from it.
 *
 * Note: this is the RAW pattern only. It does not reject reserved names and
 * does not exempt `default` — those checks live in isValidProfileName(). Callers
 * that want the full validator should use isValidProfileName / assertValidProfileName
 * directly. Use PROFILE_NAME_RE only when you intentionally want raw-pattern
 * matching (e.g. sanitizeConnectionProfiles, which accepts reserved names as
 * connection-scope keys by design).
 */
export const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

/**
 * Mirrors hermes_cli.profiles._RESERVED_NAMES — names that would collide on
 * disk (a `hermes` profile inside `~/.hermes/`) or with a system binary. Note
 * the Python set also includes `default`, but the validator exempts `default`
 * explicitly before the reserved check, so `isValidProfileName('default')`
 * returns true.
 */
const RESERVED_PROFILE_NAMES = new Set(['hermes', 'default', 'test', 'tmp', 'root', 'sudo'])

/**
 * True when `name` is a valid Hermes profile identifier that the backend's own
 * resolver will accept. Equivalent to `validate_profile_name(name)` raising
 * no ValueError.
 *
 * - `default` is accepted (special alias for ~/.hermes).
 * - Must match [a-z0-9][a-z0-9_-]{0,63}.
 * - Must not be a reserved name (hermes, test, tmp, root, sudo).
 *
 * Callers that accept mixed-case input from users must normalize (lowercase,
 * trim) before calling; this validates the on-disk identifier strictly, the
 * same way the Python validator does.
 */
export function isValidProfileName(name: string): boolean {
  if (typeof name !== 'string') {
    return false
  }

  if (name === 'default') {
    return true
  }

  if (!PROFILE_NAME_RE.test(name)) {
    return false
  }

  return !RESERVED_PROFILE_NAMES.has(name)
}

/**
 * Throw when `name` is not a valid Hermes profile identifier. Mirrors
 * validate_profile_name() raising ValueError, for call sites that prefer the
 * throw-on-invalid shape (constructor validation, bind validation).
 */
export function assertValidProfileName(name: string): void {
  if (!isValidProfileName(name)) {
    throw new Error(`Invalid profile name: ${String(name)}`)
  }
}

/** Return a canonical desktop profile name, or null for absent/malformed input. */
export function normalizeDesktopProfile(value: unknown): null | string {
  const profile = typeof value === 'string' ? value.trim().toLowerCase() : ''

  return profile && isValidProfileName(profile) ? profile : null
}