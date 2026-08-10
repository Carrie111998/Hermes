// Mirrors hermes_cli.profiles._PROFILE_ID_RE so the desktop never routes or
// spawns a backend with a profile name the Python resolver would reject.
export const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

/** Return a canonical desktop profile name, or null for absent/malformed input. */
export function normalizeDesktopProfile(value: unknown): null | string {
  const profile = typeof value === 'string' ? value.trim() : ''

  return profile && (profile === 'default' || PROFILE_NAME_RE.test(profile)) ? profile : null
}
