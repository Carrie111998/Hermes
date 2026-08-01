import fs from 'node:fs'
import path from 'node:path'

// Electron resolves the default `userData` path as
// path.join(app.getPath('appData'), productName). `productName` changed from
// "Hermes" to "Douglas Agent" (apps/desktop/package.json, commit 4c8da5049),
// so a fresh build silently points at a brand-new, empty directory while every
// existing user's real settings (connection.json, window-state.json,
// active-profile.json, native-oauth-tokens.json, ...) sit untouched under the
// old "Hermes"-named one. This module migrates that data on first launch.
//
// Deliberately does NOT hardcode any platform-specific parent directory
// (no %APPDATA%, no ~/Library/Application Support, no ~/.config literal).
// `appDataPath` must come from the caller's `app.getPath('appData')` --
// Electron's own, already-correct per-platform resolution -- so this module
// has no platform-conditional branches to test separately: it's the same
// `path.join(appDataPath, name)` on every OS, only the OS-supplied
// `appDataPath` value differs, which the tests exercise directly with
// representative Windows/macOS/Linux-shaped inputs.
const LEGACY_PRODUCT_NAME = 'Hermes'
const MIGRATION_MARKER_FILENAME = '.migrated-from-hermes'

interface MigrationDeps {
  fs?: typeof fs
  appDataPath: string
  newUserDataPath: string
  legacyProductName?: string
  now?: () => Date
}

interface MigrationResult {
  status: 'migrated' | 'skipped-both-exist' | 'skipped-neither-exists' | 'skipped-already-migrated' | 'failed'
  legacyPath: string
  newPath: string
  migratedFiles: string[]
  fileCount: number
  error?: string
}

function legacyUserDataPath(appDataPath: string, legacyProductName: string = LEGACY_PRODUCT_NAME): string {
  return path.join(appDataPath, legacyProductName)
}

function migrationMarkerPath(newUserDataPath: string): string {
  return path.join(newUserDataPath, MIGRATION_MARKER_FILENAME)
}

function dirHasEntries(fsImpl: typeof fs, dirPath: string): boolean {
  try {
    return fsImpl.readdirSync(dirPath).length > 0
  } catch {
    return false
  }
}

// Copies `src` into `dest` recursively, preserving each regular file's mode
// bits (fs.copyFileSync does NOT preserve them on its own). Critical for
// native-oauth-tokens.json, which holds encrypted OAuth credentials -- on
// POSIX a copy that lost a restrictive mode would leave the migrated file
// world/group-readable until the app happens to rewrite it. Symlinks and
// other non-regular entries are skipped (none are expected in userData).
function copyRecursivePreservingMode(
  fsImpl: typeof fs,
  src: string,
  dest: string,
  migrated: string[],
  relPath: string
): void {
  const stat = fsImpl.statSync(src)

  if (stat.isDirectory()) {
    fsImpl.mkdirSync(dest, { recursive: true })

    for (const entry of fsImpl.readdirSync(src)) {
      copyRecursivePreservingMode(fsImpl, path.join(src, entry), path.join(dest, entry), migrated, `${relPath}/${entry}`)
    }

    return
  }

  if (!stat.isFile()) {
    return
  }

  fsImpl.copyFileSync(src, dest)
  fsImpl.chmodSync(dest, stat.mode)
  migrated.push(relPath)
}

/**
 * Copy (never move) a legacy "Hermes"-named userData directory into the
 * current, correctly-resolved one, exactly once. Never deletes or modifies
 * the legacy directory -- a failed or unwanted migration always leaves the
 * user able to fall back to the previous app version with their data intact.
 */
function migrateUserDataFromLegacyHermes(deps: MigrationDeps): MigrationResult {
  const fsImpl = deps.fs || fs
  const legacyPath = legacyUserDataPath(deps.appDataPath, deps.legacyProductName)
  const newPath = deps.newUserDataPath
  const markerPath = migrationMarkerPath(newPath)
  const empty = (status: MigrationResult['status']): MigrationResult => ({
    status,
    legacyPath,
    newPath,
    migratedFiles: [],
    fileCount: 0
  })

  if (fsImpl.existsSync(markerPath)) {
    return empty('skipped-already-migrated')
  }

  if (!dirHasEntries(fsImpl, legacyPath)) {
    return empty('skipped-neither-exists')
  }

  if (dirHasEntries(fsImpl, newPath)) {
    return empty('skipped-both-exist')
  }

  const migrated: string[] = []

  try {
    fsImpl.mkdirSync(newPath, { recursive: true })

    for (const entry of fsImpl.readdirSync(legacyPath)) {
      copyRecursivePreservingMode(fsImpl, path.join(legacyPath, entry), path.join(newPath, entry), migrated, entry)
    }

    const now = (deps.now || (() => new Date()))()

    fsImpl.writeFileSync(
      markerPath,
      JSON.stringify(
        {
          migratedFrom: legacyPath,
          migratedAt: now.toISOString(),
          fileCount: migrated.length,
          files: migrated
        },
        null,
        2
      )
    )

    return { status: 'migrated', legacyPath, newPath, migratedFiles: migrated, fileCount: migrated.length }
  } catch (error) {
    return {
      status: 'failed',
      legacyPath,
      newPath,
      migratedFiles: migrated,
      fileCount: migrated.length,
      error: error instanceof Error ? error.message : String(error)
    }
  }
}

export {
  copyRecursivePreservingMode,
  legacyUserDataPath,
  LEGACY_PRODUCT_NAME,
  migrateUserDataFromLegacyHermes,
  migrationMarkerPath,
  MIGRATION_MARKER_FILENAME
}
export type { MigrationDeps, MigrationResult }
