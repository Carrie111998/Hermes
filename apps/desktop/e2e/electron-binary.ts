import { spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'

/**
 * Platform-correct filename/layout for the electron npm package's bundled
 * binary (#92846): `electron.exe` on Windows, an app bundle on macOS, and a
 * plain executable on Linux.
 */
function bundledBinaryName(platform: string): string | null {
  if (platform === 'win32') {
    return 'electron.exe'
  }

  if (platform === 'darwin') {
    return path.join('Electron.app', 'Contents', 'MacOS', 'Electron')
  }

  return 'electron'
}

/**
 * Candidate paths for the Electron dev binary, most-preferred first.
 *
 * Two real layouts exist (#92846): npm may hoist electron to the repo root
 * (classic POSIX workspaces), or the desktop package may own its own copy
 * under apps/desktop/node_modules (the normal layout on Windows).
 */
export function electronBinaryCandidates(
  repoRoot: string,
  desktopRoot: string,
  platform: string = process.platform,
): string[] {
  const exe = bundledBinaryName(platform)

  if (!exe) {
    return []
  }

  return [
    path.join(repoRoot, 'node_modules', 'electron', 'dist', exe),
    path.join(desktopRoot, 'node_modules', 'electron', 'dist', exe),
  ]
}

/**
 * Find the Electron dev binary: node_modules layouts first (hoisted, then
 * desktop-local), then PATH (`where` on Windows, `which` on POSIX).
 *
 * Lives outside fixtures.ts so it stays importable from unit tests —
 * fixtures.ts registers Playwright hooks at import time.
 */
export function findElectronBinary(
  repoRoot: string,
  desktopRoot: string,
  platform: string = process.platform,
): string {
  for (const candidate of electronBinaryCandidates(repoRoot, desktopRoot, platform)) {
    if (fs.existsSync(candidate)) {
      return candidate
    }
  }

  const lookup = platform === 'win32' ? 'where' : 'which'

  const result = spawnSync(lookup, ['electron'], {
    encoding: 'utf8',
  })

  if (result.status === 0 && result.stdout.trim()) {
    // `where` may report several matches; any first line is a usable binary.
    const firstMatch = result.stdout.trim().split(/\r?\n/)[0].trim()

    if (firstMatch) {
      return firstMatch
    }
  }

  throw new Error(
    'Electron binary not found. Run "npm install" from the repo root to install devDependencies.',
  )
}
