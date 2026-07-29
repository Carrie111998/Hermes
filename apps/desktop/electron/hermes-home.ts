import path from 'node:path'

import { normalizeHermesHomeRoot } from './backend-env'
import { readWindowsUserEnvVar } from './windows-user-env'

interface ResolveHermesHomeOptions {
  directoryExists: (path: string) => boolean
  homeDirectory: string
  isWindows: boolean
  localAppData?: string
  userDataOverride?: string | null
  env?: NodeJS.ProcessEnv
}

function normalizeHome(hermesHome: string, isWindows: boolean): string {
  return normalizeHermesHomeRoot(hermesHome, { pathModule: isWindows ? path.win32 : path.posix })
}

function resolveHermesHome({
  directoryExists,
  homeDirectory,
  isWindows,
  localAppData,
  userDataOverride,
  env = process.env
}: ResolveHermesHomeOptions): string {
  const pathModule = isWindows ? path.win32 : path.posix
  const configuredHome = env.HERMES_HOME
  if (configuredHome) {
    return normalizeHome(configuredHome, isWindows)
  }

  if (userDataOverride) {
    return pathModule.join(pathModule.resolve(userDataOverride), 'hermes-home')
  }

  if (isWindows) {
    // A GUI app launched from Explorer inherits the environment block captured
    // at login, so a HERMES_HOME set via `setx` AFTER login is invisible in
    // process.env even though the CLI (a fresh shell) sees it. Without this the
    // backend silently falls back to %LOCALAPPDATA%\hermes and reports "No
    // inference provider configured" despite a valid configured home (#45471).
    // Consult the live User-scoped registry value before the default below.
    const fromRegistry = readWindowsUserEnvVar('HERMES_HOME')
    if (fromRegistry) {
      return normalizeHome(fromRegistry, isWindows)
    }
  }

  if (isWindows && localAppData) {
    const localAppDataHome = pathModule.join(localAppData, 'hermes')
    const legacyHome = pathModule.join(homeDirectory, '.hermes')

    // Migrate transparently to LOCALAPPDATA, but honour an existing legacy
    // ~/.hermes setup (no LOCALAPPDATA install yet) so users don't lose state.
    return !directoryExists(localAppDataHome) && directoryExists(legacyHome) ? legacyHome : localAppDataHome
  }

  return pathModule.join(homeDirectory, '.hermes')
}

export { resolveHermesHome }
