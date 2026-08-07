/**
 * Unit tests for chooseWindowsHermesHome — the Windows Hermes home
 * resolution helper extracted from resolveHermesHome() in main.ts.
 *
 * These test the pure decision function independently of Electron,
 * so they run on any platform.
 */
import { describe, it, expect } from 'vitest'
import path from 'node:path'
import { chooseWindowsHermesHome } from './hermes-home'

// ---------------------------------------------------------------------------
// Test fixtures
// ---------------------------------------------------------------------------
const LOCAL = 'C:\\Users\\test\\AppData\\Local\\hermes'
const LEGACY = 'C:\\Users\\test\\.hermes'

function makeProbes(existingFiles: string[], existingDirs: string[]) {
  return {
    fileExists: (p: string) => existingFiles.includes(p),
    directoryExists: (p: string) => existingDirs.includes(p),
  }
}

describe('chooseWindowsHermesHome', () => {
  // -----------------------------------------------------------------------
  // Established desktop install — always wins
  // -----------------------------------------------------------------------
  it('returns LOCALAPPDATA when both sides have state.db (desktop wins)', () => {
    const probes = makeProbes(
      [path.join(LOCAL, 'state.db'), path.join(LEGACY, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, probes)).toBe(LOCAL)
  })

  it('returns LOCALAPPDATA when only LOCALAPPDATA has state.db', () => {
    const probes = makeProbes(
      [path.join(LOCAL, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, probes)).toBe(LOCAL)
  })

  // -----------------------------------------------------------------------
  // CLI-first user — the #40178 scenario
  // -----------------------------------------------------------------------
  it('returns legacy when only legacy has state.db (orphaned CLI data)', () => {
    // Installer pre-created LOCALAPPDATA dir (empty), legacy has real sessions.
    const probes = makeProbes(
      [path.join(LEGACY, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, probes)).toBe(LEGACY)
  })

  it('returns legacy when LOCALAPPDATA dir is missing entirely and legacy has state.db', () => {
    // Test gap noted in #40233 review: LOCALAPPDATA does not exist as a
    // directory, but legacy has real data. DB check fires before the dir
    // heuristic, so legacy wins.
    const probes = makeProbes(
      [path.join(LEGACY, 'state.db')],
      [LEGACY],  // LOCALAPPDATA dir does not exist
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, probes)).toBe(LEGACY)
  })

  // -----------------------------------------------------------------------
  // Fresh install — original heuristic preserved
  // -----------------------------------------------------------------------
  it('returns legacy when LOCALAPPDATA missing and legacy dir exists (fresh heuristic)', () => {
    const probes = makeProbes([], [LEGACY])
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, probes)).toBe(LEGACY)
  })

  it('returns LOCALAPPDATA for a truly fresh install (no dirs, no dbs)', () => {
    const probes = makeProbes([], [])
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, probes)).toBe(LOCAL)
  })

  it('returns LOCALAPPDATA when installer pre-created dir but no DB on either side', () => {
    // Both dirs exist but neither has state.db — config/.env-only legacy
    // setup. Original heuristic picks LOCALAPPDATA because it exists.
    const probes = makeProbes([], [LOCAL, LEGACY])
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, probes)).toBe(LOCAL)
  })

  // -----------------------------------------------------------------------
  // Edge cases
  // -----------------------------------------------------------------------
  it('returns LOCALAPPDATA when local DB exists even if legacy also exists', () => {
    // Desktop install established → never silently switch back to CLI data.
    const probes = makeProbes(
      [path.join(LOCAL, 'state.db'), path.join(LEGACY, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, probes)).toBe(LOCAL)
  })

  it('returns legacy when LOCALAPPDATA dir exists but empty, legacy has DB', () => {
    // Exact #40178: installer created LOCALAPPDATA dir (empty),
    // legacy has real CLI sessions.
    const probes = makeProbes(
      [path.join(LEGACY, 'state.db')],
      [LOCAL, LEGACY],
    )
    expect(chooseWindowsHermesHome(LOCAL, LEGACY, probes)).toBe(LEGACY)
  })
})
