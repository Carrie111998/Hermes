import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { chooseWindowsDefaultHermesHome, HERMES_HOME_CONFIG_MARKERS, isConfiguredHermesHome } from './hermes-home'

const LOCALAPPDATA_HOME = 'C:\\Users\\jeff\\AppData\\Local\\hermes'
const LEGACY_HOME = 'C:\\Users\\jeff\\.hermes'

// Build fs probes from explicit lists: `dirs` exist as directories, `files`
// exist as files. Paths compare after normalization so tests can mix
// path.join output with literals.
function fsProbes({ dirs = [], files = [] }: { dirs?: string[]; files?: string[] }) {
  const norm = (p: string) => path.normalize(p).toLowerCase()
  const dirSet = new Set(dirs.map(norm))
  const fileSet = new Set(files.map(norm))

  return {
    directoryExists: (p: string) => dirSet.has(norm(p)),
    fileExists: (p: string) => fileSet.has(norm(p))
  }
}

// ── isConfiguredHermesHome ─────────────────────────────────────────────────

test('isConfiguredHermesHome accepts any single config marker', () => {
  for (const marker of HERMES_HOME_CONFIG_MARKERS) {
    const { fileExists } = fsProbes({ files: [path.join(LEGACY_HOME, marker)] })
    assert.equal(isConfiguredHermesHome(LEGACY_HOME, { fileExists }), true, `marker ${marker} should qualify`)
  }
})

test('isConfiguredHermesHome rejects a home with none of the markers', () => {
  // The 2026-08 stray %LOCALAPPDATA%\hermes had sessions/, cron/, logs/,
  // jobguard/ … but no config marker. Bare state a backend spawned into a
  // default location must NOT read as a configured home.
  const { fileExists } = fsProbes({
    files: [path.join(LOCALAPPDATA_HOME, 'gateway_state.json'), path.join(LOCALAPPDATA_HOME, 'SOUL.md')]
  })

  assert.equal(isConfiguredHermesHome(LOCALAPPDATA_HOME, { fileExists }), false)
})

// ── chooseWindowsDefaultHermesHome ─────────────────────────────────────────

function choose(probes: ReturnType<typeof fsProbes>) {
  return chooseWindowsDefaultHermesHome({
    localAppDataHome: LOCALAPPDATA_HOME,
    legacyHome: LEGACY_HOME,
    ...probes
  })
}

test('fresh install (neither home exists) → LOCALAPPDATA, no warning', () => {
  const decision = choose(fsProbes({}))

  assert.equal(decision.home, LOCALAPPDATA_HOME)
  assert.equal(decision.reason, 'fresh-install')
  assert.equal(decision.warning, null)
})

test('only LOCALAPPDATA exists → LOCALAPPDATA, no warning', () => {
  const decision = choose(fsProbes({ dirs: [LOCALAPPDATA_HOME] }))

  assert.equal(decision.home, LOCALAPPDATA_HOME)
  assert.equal(decision.reason, 'localappdata-only')
  assert.equal(decision.warning, null)
})

test('only legacy ~/.hermes exists → legacy, no warning', () => {
  const decision = choose(fsProbes({ dirs: [LEGACY_HOME], files: [path.join(LEGACY_HOME, 'config.yaml')] }))

  assert.equal(decision.home, LEGACY_HOME)
  assert.equal(decision.reason, 'legacy-only')
  assert.equal(decision.warning, null)
})

test('REGRESSION #08-31: stray unconfigured LOCALAPPDATA must not shadow a configured legacy home', () => {
  // Exact shape of the 2026-08-13 / 2026-08-31 incidents: a nearly-empty
  // %LOCALAPPDATA%\hermes (created by a backend once spawned against the
  // default) exists alongside the real, fully configured ~/.hermes. The old
  // code returned LOCALAPPDATA on mere existence and the sidebar showed an
  // alien 1-session store.
  const decision = choose(
    fsProbes({
      dirs: [LOCALAPPDATA_HOME, LEGACY_HOME],
      files: [path.join(LEGACY_HOME, 'config.yaml'), path.join(LEGACY_HOME, 'auth.json')]
    })
  )

  assert.equal(decision.home, LEGACY_HOME)
  assert.equal(decision.reason, 'legacy-configured')
  // The silent fallback cost two multi-hour investigations — the ambiguity
  // must be surfaced, naming both candidate paths.
  assert.ok(decision.warning, 'expected a warning about the ignored LOCALAPPDATA home')
  assert.ok(decision.warning.includes(LOCALAPPDATA_HOME))
  assert.ok(decision.warning.includes(LEGACY_HOME))
})

test('configured LOCALAPPDATA beside an unconfigured legacy dir → LOCALAPPDATA, no warning', () => {
  // A real migrated install: LOCALAPPDATA is the configured home, a remnant
  // ~/.hermes directory lingers without config. Not ambiguous.
  const decision = choose(
    fsProbes({
      dirs: [LOCALAPPDATA_HOME, LEGACY_HOME],
      files: [path.join(LOCALAPPDATA_HOME, 'config.yaml')]
    })
  )

  assert.equal(decision.home, LOCALAPPDATA_HOME)
  assert.equal(decision.reason, 'localappdata-configured')
  assert.equal(decision.warning, null)
})

test('both configured → LOCALAPPDATA (documented install.ps1 default) with a warning', () => {
  const decision = choose(
    fsProbes({
      dirs: [LOCALAPPDATA_HOME, LEGACY_HOME],
      files: [path.join(LOCALAPPDATA_HOME, 'config.yaml'), path.join(LEGACY_HOME, 'config.yaml')]
    })
  )

  assert.equal(decision.home, LOCALAPPDATA_HOME)
  assert.equal(decision.reason, 'both-configured')
  assert.ok(decision.warning)
  assert.ok(decision.warning.includes(LEGACY_HOME))
})

test('both exist, neither configured → LOCALAPPDATA with a warning', () => {
  const decision = choose(fsProbes({ dirs: [LOCALAPPDATA_HOME, LEGACY_HOME] }))

  assert.equal(decision.home, LOCALAPPDATA_HOME)
  assert.equal(decision.reason, 'neither-configured')
  assert.ok(decision.warning)
})
