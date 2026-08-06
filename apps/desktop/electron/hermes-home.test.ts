/**
 * Unit tests for the Windows Hermes home resolution logic extracted
 * from resolveHermesHome() in main.ts (#40178).
 *
 * These test the pure decision function independently of Electron,
 * so they run on any platform.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import path from 'node:path'

// ---------------------------------------------------------------------------
// Pure logic under test (extracted from resolveHermesHome, main.ts)
// ---------------------------------------------------------------------------
function chooseWindowsHermesHome(
  localappdata: string,
  legacy: string,
  {
    fileExists,
    directoryExists,
  }: {
    fileExists: (p: string) => boolean
    directoryExists: (p: string) => boolean
  },
): string {
  const localDb = path.join(localappdata, 'state.db')
  const legacyDb = path.join(legacy, 'state.db')
  const localHasData = fileExists(localDb)
  const legacyHasData = fileExists(legacyDb)

  // An established desktop install (state.db present) always wins —
  // never hijack back to legacy.
  if (localHasData) {
    return localappdata
  }

  // Only legacy has real data: honour the CLI-first user's setup.
  if (legacyHasData) {
    return legacy
  }

  // Neither has real data — fresh install.
  if (!directoryExists(localappdata) && directoryExists(legacy)) {
    return legacy
  }

  return localappdata
}

// ---------------------------------------------------------------------------
// Test cases
// ---------------------------------------------------------------------------
const LOCAL = 'C:\\Users\\test\\AppData\\Local\\hermes'
const LEGACY = 'C:\\Users\\test\\.hermes'

function makeFs(existingFiles: string[], existingDirs: string[]) {
  return {
    fileExists: (p: string) => existingFiles.includes(p),
    directoryExists: (p: string) => existingDirs.includes(p),
  }
}

describe('chooseWindowsHermesHome', () => {
  it('returns LOCALAPPDATA when desktop has an established state.db', () => {
    const fs = makeFs(
      [path.join(LOCAL, 'state.db'), path.join(LEGACY, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, fs)).toBe(LOCAL)
  })

  it('returns LOCALAPPDATA when only LOCALAPPDATA has state.db', () => {
    const fs = makeFs(
      [path.join(LOCAL, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, fs)).toBe(LOCAL)
  })

  it('returns legacy when only legacy has state.db (the orphaned-CLI case)', () => {
    const fs = makeFs(
      [path.join(LEGACY, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, fs)).toBe(LEGACY)
  })

  it('returns legacy when LOCALAPPDATA is missing and legacy dir exists (fresh install heuristic)', () => {
    const fs = makeFs([], [LEGACY])
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, fs)).toBe(LEGACY)
  })

  it('returns LOCALAPPDATA for a truly fresh install (no dirs, no dbs)', () => {
    const fs = makeFs([], [])
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, fs)).toBe(LOCAL)
  })

  it('returns LOCALAPPDATA when installer pre-created the dir but no DB exists on either side', () => {
    // This is the exact scenario #40178: the installer created
    // %LOCALAPPDATA%\hermes (empty), legacy ~/.hermes exists but has no
    // state.db (e.g. a pip install that never ran). Original heuristic
    // would return LOCALAPPDATA; we keep that for the no-data case.
    const fs = makeFs([], [LOCAL, LEGACY])
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, fs)).toBe(LOCAL)
  })

  it('returns LOCALAPPDATA when local DB exists even if legacy also exists', () => {
    // Desktop install established → never silently switch back to CLI data.
    const fs = makeFs(
      [path.join(LOCAL, 'state.db'), path.join(LEGACY, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, fs)).toBe(LOCAL)
  })

  it('returns legacy when LOCALAPPDATA dir exists but empty and legacy has data', () => {
    // The exact #40178 scenario: installer created the LOCALAPPDATA dir
    // but it's empty; legacy has real CLI sessions.
    const fs = makeFs(
      [path.join(LEGACY, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, fs)).toBe(LEGACY)
  })
})
