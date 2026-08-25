import path from 'node:path'

/**
 * Foundrly keeps an isolated HERMES_HOME (QuizVerse pattern) so left-rail
 * Hermes chat does not inherit IX Agency SOUL.md / keys / sessions from the
 * shared %LOCALAPPDATA%\hermes (or ~/.hermes) default.
 */

export function assertFoundrlyIsolatedHome(candidate: string, ixDefault: string): string {
  const resolved = path.resolve(candidate)

  if (resolved === path.resolve(ixDefault)) {
    throw new Error(
      'Foundrly refuses the shared IX/default HERMES_HOME. Unset HERMES_HOME or migrate to a dedicated Foundrly path.'
    )
  }

  return resolved
}

export function resolveFoundrlyEffectiveHermesHome(
  baseHome: string,
  activeProfile: string | null
): string {
  return activeProfile && activeProfile !== 'default'
    ? path.join(baseHome, 'profiles', activeProfile)
    : baseHome
}
