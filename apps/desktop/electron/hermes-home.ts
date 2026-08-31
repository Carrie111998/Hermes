// hermes-home.ts
//
// Decide which directory is the Windows-default HERMES_HOME when nothing pins
// it explicitly (no HERMES_HOME in process env or the HKCU registry, no
// userData override). Pulled out of main.ts's resolveHermesHome() so the
// decision is unit-testable, same pattern as windows-hermes-path.ts.
//
// History: resolveHermesHome() used to return %LOCALAPPDATA%\hermes whenever
// that directory merely EXISTED, honoring a populated legacy ~/.hermes only
// when the LOCALAPPDATA one was absent. Any backend once spawned against the
// default (or a partial install) leaves a stray, nearly-empty
// %LOCALAPPDATA%\hermes behind, which then silently captured every later
// launch: the sidebar showed an alien 1-session store while the real, fully
// configured ~/.hermes sat ignored. The fallback was silent, so this cost two
// multi-hour investigations (2026-08-13 and 2026-08-31) of "my sessions
// disappeared". When both candidates exist, prefer the one that looks like a
// real configured Hermes home — and always surface the ambiguity.

import fs from 'node:fs'
import path from 'node:path'

// Files whose presence marks a directory as a deliberately configured Hermes
// home rather than bare state a backend spawned into a default location.
// (config.yaml is written by initial configuration; auth.json and .env by
// provider setup.) Deliberately cheap existence probes — no sqlite opens at
// startup; a stray home has sessions/, cron/, logs/ etc. but none of these.
const HERMES_HOME_CONFIG_MARKERS = ['config.yaml', 'auth.json', '.env']

function defaultFileExists(p: string) {
  try {
    return fs.statSync(p).isFile()
  } catch {
    return false
  }
}

function defaultDirectoryExists(p: string) {
  try {
    return fs.statSync(p).isDirectory()
  } catch {
    return false
  }
}

// True when `dir` carries at least one config marker.
function isConfiguredHermesHome(
  dir: string,
  { fileExists = defaultFileExists }: { fileExists?: (p: string) => boolean } = {}
) {
  return HERMES_HOME_CONFIG_MARKERS.some(marker => fileExists(path.join(dir, marker)))
}

type HermesHomeDecision = {
  home: string
  reason:
    | 'fresh-install'
    | 'localappdata-only'
    | 'legacy-only'
    | 'localappdata-configured'
    | 'legacy-configured'
    | 'both-configured'
    | 'neither-configured'
  // Non-null whenever the choice deserves operator attention (both candidates
  // exist and the winner wasn't uniquely determined by config markers, or the
  // documented %LOCALAPPDATA% default lost to the legacy home). Callers must
  // log it — the silent version of this decision is what made the stray-home
  // failures cost hours.
  warning: string | null
}

// Pick between the two Windows HERMES_HOME candidates.
//
//   - only one exists → that one (fresh installs get %LOCALAPPDATA%\hermes,
//     matching install.ps1).
//   - both exist → the configured one wins; ties keep the documented
//     %LOCALAPPDATA% default but WARN, because the situation is ambiguous and
//     someone's sessions live in only one of them.
function chooseWindowsDefaultHermesHome({
  localAppDataHome,
  legacyHome,
  directoryExists = defaultDirectoryExists,
  fileExists = defaultFileExists
}: {
  localAppDataHome: string
  legacyHome: string
  directoryExists?: (p: string) => boolean
  fileExists?: (p: string) => boolean
}): HermesHomeDecision {
  const localExists = directoryExists(localAppDataHome)
  const legacyExists = directoryExists(legacyHome)

  if (!localExists && !legacyExists) {
    return { home: localAppDataHome, reason: 'fresh-install', warning: null }
  }

  if (localExists && !legacyExists) {
    return { home: localAppDataHome, reason: 'localappdata-only', warning: null }
  }

  if (!localExists && legacyExists) {
    return { home: legacyHome, reason: 'legacy-only', warning: null }
  }

  const localConfigured = isConfiguredHermesHome(localAppDataHome, { fileExists })
  const legacyConfigured = isConfiguredHermesHome(legacyHome, { fileExists })

  if (localConfigured && !legacyConfigured) {
    return { home: localAppDataHome, reason: 'localappdata-configured', warning: null }
  }

  if (legacyConfigured && !localConfigured) {
    return {
      home: legacyHome,
      reason: 'legacy-configured',
      warning:
        `both ${localAppDataHome} and ${legacyHome} exist; using the legacy home because it is ` +
        `configured (${HERMES_HOME_CONFIG_MARKERS.join('/')}) and the LOCALAPPDATA one is not — ` +
        `it looks like stray state from a backend once launched against the default. ` +
        `Set HERMES_HOME explicitly (or remove the stray directory) to silence this.`
    }
  }

  return {
    home: localAppDataHome,
    reason: localConfigured ? 'both-configured' : 'neither-configured',
    warning:
      `both ${localAppDataHome} and ${legacyHome} exist and ` +
      `${localConfigured ? 'BOTH carry' : 'NEITHER carries'} a config marker ` +
      `(${HERMES_HOME_CONFIG_MARKERS.join('/')}); defaulting to ${localAppDataHome}. ` +
      `If your sessions live in ${legacyHome}, set HERMES_HOME explicitly.`
  }
}

export { chooseWindowsDefaultHermesHome, HERMES_HOME_CONFIG_MARKERS, isConfiguredHermesHome }
export type { HermesHomeDecision }
