/**
 * Pure helper for choosing the Windows Hermes home directory.
 *
 * Extracted from ``resolveHermesHome()`` in ``main.ts`` so the decision
 * table is trivially unit-testable without booting Electron (#40178).
 *
 * Accepts injected ``fileExists`` / ``directoryExists`` probes so tests
 * can supply a fake filesystem.
 */
import path from 'node:path'

export function chooseWindowsHermesHome(
  localappdata: string,
  legacy: string,
  probes: {
    fileExists: (p: string) => boolean
    directoryExists: (p: string) => boolean
  },
): string {
  const localDb = path.join(localappdata, 'state.db')
  const legacyDb = path.join(legacy, 'state.db')
  const localHasData = probes.fileExists(localDb)
  const legacyHasData = probes.fileExists(legacyDb)

  // An established desktop install (state.db present) always wins —
  // never hijack back to legacy.
  if (localHasData) {
    return localappdata
  }

  // Only legacy has real data: honour the CLI-first user's setup
  // even if the installer pre-created an empty LOCALAPPDATA dir.
  if (legacyHasData) {
    return legacy
  }

  // Neither has real data — fresh install.  Preserve the original
  // heuristic so config / .env files in a pre-session legacy dir
  // aren't orphaned.
  if (!probes.directoryExists(localappdata) && probes.directoryExists(legacy)) {
    return legacy
  }

  return localappdata
}
