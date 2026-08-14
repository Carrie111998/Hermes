import fs from 'node:fs'
import path from 'node:path'

// Mirrors hermes_cli.profiles._PROFILE_ID_RE so the Desktop never persists a
// value the profile resolver would reject during the next backend launch.
export const DESKTOP_PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

function normalizeDesktopPrimaryProfile(name: unknown): string | null {
  const value = typeof name === 'string' ? name.trim() : ''

  if (value && value !== 'default' && !DESKTOP_PROFILE_NAME_RE.test(value)) {
    throw new Error(`Invalid profile name: ${value}`)
  }

  return value || null
}

// Returns the persisted Desktop primary profile. Explicit "default" pins the
// root HERMES_HOME; null preserves the legacy launch without a --profile flag.
export function readDesktopPrimaryProfile(configPath: string): string | null {
  try {
    const parsed = JSON.parse(fs.readFileSync(configPath, 'utf8'))
    const name = parsed && typeof parsed.profile === 'string' ? parsed.profile.trim() : ''

    if (name && (name === 'default' || DESKTOP_PROFILE_NAME_RE.test(name))) {
      return name
    }
  } catch {
    // Missing or malformed means there is no persisted preference.
  }

  return null
}

export function writeDesktopPrimaryProfile(configPath: string, name: unknown): string | null {
  const value = normalizeDesktopPrimaryProfile(name)
  const temporaryPath = `${configPath}.tmp`

  fs.mkdirSync(path.dirname(configPath), { recursive: true })
  fs.writeFileSync(temporaryPath, JSON.stringify({ profile: value }, null, 2))
  fs.renameSync(temporaryPath, configPath)

  return value
}

interface DesktopPrimaryProfileLifecycle {
  reload: () => void
  teardownPrimary: () => Promise<void>
}

// Persist first, then re-home the primary backend and reload its renderer. Do
// not infer the running backend from the file: a prior teardown may have failed
// after persistence, and a retry still needs to complete the re-home.
export async function applyDesktopPrimaryProfile(
  configPath: string,
  name: unknown,
  lifecycle: DesktopPrimaryProfileLifecycle
): Promise<string | null> {
  const next = normalizeDesktopPrimaryProfile(name)

  writeDesktopPrimaryProfile(configPath, next)
  await lifecycle.teardownPrimary()
  lifecycle.reload()

  return next
}
