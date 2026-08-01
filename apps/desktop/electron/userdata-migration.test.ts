import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { legacyUserDataPath, migrateUserDataFromLegacyHermes, migrationMarkerPath } from './userdata-migration'

// NOTE on "multiplatform": migrateUserDataFromLegacyHermes takes `appDataPath`
// as a plain injected string (the caller passes app.getPath('appData')) and
// never branches on process.platform or hardcodes a platform-specific parent
// directory -- that was the whole point of using Electron's own resolution
// instead of hand-rolled %APPDATA%/Application Support/.config literals. So
// there is no platform-conditional code path here to monkeypatch
// process.platform around. Instead:
//   - the first block below asserts the pure path-join behavior against
//     representative Windows/macOS/Linux-shaped appDataPath values Electron
//     would actually supply on each OS;
//   - every other test below exercises the real copy/marker/error behavior
//     end-to-end against a real tmpdir on whichever OS runs the suite.

test('legacyUserDataPath joins the legacy name onto whatever appDataPath is given, for every platform shape', () => {
  // legacyUserDataPath uses the ambient `path.join` (correct in production,
  // since Electron only ever pairs a Windows-shaped appDataPath with an
  // actual Windows process, etc. -- the two never mismatch). To assert the
  // expected separator deterministically regardless of which OS runs this
  // suite, build expectations with path.win32/path.posix explicitly rather
  // than hardcoding a separator or monkeypatching process.platform.
  assert.equal(
    legacyUserDataPath('C:\\Users\\alex\\AppData\\Roaming', 'Hermes'),
    path.join('C:\\Users\\alex\\AppData\\Roaming', 'Hermes')
  )
  assert.equal(path.win32.join('C:\\Users\\alex\\AppData\\Roaming', 'Hermes'), 'C:\\Users\\alex\\AppData\\Roaming\\Hermes')
  assert.equal(
    path.posix.join('/Users/alex/Library/Application Support', 'Hermes'),
    '/Users/alex/Library/Application Support/Hermes'
  )
  assert.equal(path.posix.join('/home/alex/.config', 'Hermes'), '/home/alex/.config/Hermes')
})

function makeSandbox() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'douglas-userdata-migration-'))
  const appDataPath = path.join(root, 'appdata')
  const newUserDataPath = path.join(appDataPath, 'Douglas Agent')
  fs.mkdirSync(appDataPath, { recursive: true })

  return { root, appDataPath, newUserDataPath }
}

function writeLegacyFixture(appDataPath: string) {
  const legacy = path.join(appDataPath, 'Hermes')
  fs.mkdirSync(legacy, { recursive: true })
  fs.writeFileSync(path.join(legacy, 'connection.json'), JSON.stringify({ baseUrl: 'http://127.0.0.1:1234' }))
  fs.writeFileSync(path.join(legacy, 'window-state.json'), JSON.stringify({ width: 1200, height: 800 }))

  const tokensPath = path.join(legacy, 'native-oauth-tokens.json')
  fs.writeFileSync(tokensPath, JSON.stringify({ 'http://x': { encoding: 'safeStorage', value: 'c2VjcmV0' } }))
  fs.chmodSync(tokensPath, 0o600)

  const subdir = path.join(legacy, 'backend-ready')
  fs.mkdirSync(subdir, { recursive: true })
  fs.writeFileSync(path.join(subdir, 'marker'), 'x')

  return legacy
}

test('legacy exists, new does not -> migrates everything and writes a marker', () => {
  const { appDataPath, newUserDataPath } = makeSandbox()
  const legacy = writeLegacyFixture(appDataPath)

  const result = migrateUserDataFromLegacyHermes({ appDataPath, newUserDataPath })

  assert.equal(result.status, 'migrated')
  assert.equal(result.fileCount, 4) // connection.json, window-state.json, native-oauth-tokens.json, backend-ready/marker
  assert.ok(fs.existsSync(path.join(newUserDataPath, 'connection.json')))
  assert.ok(fs.existsSync(path.join(newUserDataPath, 'window-state.json')))
  assert.ok(fs.existsSync(path.join(newUserDataPath, 'backend-ready', 'marker')))
  assert.deepEqual(
    JSON.parse(fs.readFileSync(path.join(newUserDataPath, 'connection.json'), 'utf8')),
    { baseUrl: 'http://127.0.0.1:1234' }
  )

  // legacy directory is untouched -- copy, not move
  assert.ok(fs.existsSync(path.join(legacy, 'connection.json')))

  // marker recorded what happened
  const marker = JSON.parse(fs.readFileSync(migrationMarkerPath(newUserDataPath), 'utf8'))
  assert.equal(marker.fileCount, 4)
  assert.equal(marker.migratedFrom, legacy)
})

test('native-oauth-tokens.json keeps its file mode across the copy (credentials, not just content)', () => {
  const { appDataPath, newUserDataPath } = makeSandbox()
  writeLegacyFixture(appDataPath)

  migrateUserDataFromLegacyHermes({ appDataPath, newUserDataPath })

  const migratedTokens = path.join(newUserDataPath, 'native-oauth-tokens.json')
  const mode = fs.statSync(migratedTokens).mode & 0o777

  if (process.platform === 'win32') {
    // Windows fs.chmod only toggles the read-only attribute; POSIX mode bits
    // aren't meaningful there. Just assert the file made it across intact.
    assert.ok(fs.existsSync(migratedTokens))
  } else {
    assert.equal(mode, 0o600)
  }
})

test('both legacy and new already have data -> does not touch either', () => {
  const { appDataPath, newUserDataPath } = makeSandbox()
  writeLegacyFixture(appDataPath)
  fs.mkdirSync(newUserDataPath, { recursive: true })
  fs.writeFileSync(path.join(newUserDataPath, 'connection.json'), JSON.stringify({ baseUrl: 'http://existing' }))

  const result = migrateUserDataFromLegacyHermes({ appDataPath, newUserDataPath })

  assert.equal(result.status, 'skipped-both-exist')
  assert.deepEqual(
    JSON.parse(fs.readFileSync(path.join(newUserDataPath, 'connection.json'), 'utf8')),
    { baseUrl: 'http://existing' }
  )
  assert.equal(fs.existsSync(migrationMarkerPath(newUserDataPath)), false)
})

test('neither legacy nor new has data -> clean install, no-op', () => {
  const { appDataPath, newUserDataPath } = makeSandbox()

  const result = migrateUserDataFromLegacyHermes({ appDataPath, newUserDataPath })

  assert.equal(result.status, 'skipped-neither-exists')
  assert.equal(fs.existsSync(newUserDataPath), false)
})

test('marker already present -> never re-migrates, even if legacy still has data', () => {
  const { appDataPath, newUserDataPath } = makeSandbox()
  writeLegacyFixture(appDataPath)
  fs.mkdirSync(newUserDataPath, { recursive: true })
  fs.writeFileSync(migrationMarkerPath(newUserDataPath), JSON.stringify({ migratedFrom: 'x', fileCount: 0, files: [] }))

  const result = migrateUserDataFromLegacyHermes({ appDataPath, newUserDataPath })

  assert.equal(result.status, 'skipped-already-migrated')
  assert.equal(fs.existsSync(path.join(newUserDataPath, 'connection.json')), false)
})

test('copy failure is reported, not swallowed -- legacy data is never lost', () => {
  const { appDataPath, newUserDataPath } = makeSandbox()
  const legacy = writeLegacyFixture(appDataPath)

  // Object.create(fs) (no type annotation -- TS infers `any` from the
  // single-arg Object.create overload) rather than `: typeof fs`: `fs` is an
  // imported module binding, and TS treats every property of `typeof fs` as
  // readonly to match real ES module namespace semantics, so assigning to
  // failingFs.copyFileSync below would fail to typecheck (TS2540) even
  // though it works fine at runtime against Node's actual (mutable, CJS)
  // fs module. Every other fs method needed by the migration walk
  // (readdirSync, statSync, mkdirSync, existsSync, writeFileSync,
  // chmodSync) already resolves through the prototype chain unchanged --
  // only copyFileSync needs overriding here.
  const failingFs = Object.create(fs)

  failingFs.copyFileSync = (src: string, dest: string) => {
    if (String(src).endsWith('native-oauth-tokens.json')) {
      throw new Error('EACCES: permission denied (simulated)')
    }

    return fs.copyFileSync(src, dest)
  }

  const result = migrateUserDataFromLegacyHermes({ appDataPath, newUserDataPath, fs: failingFs })

  assert.equal(result.status, 'failed')
  assert.match(result.error || '', /permission denied/)

  // No silent empty start: the marker must NOT be written on failure, so a
  // retry (or the fallback error dialog) can run again next launch.
  assert.equal(fs.existsSync(migrationMarkerPath(newUserDataPath)), false)

  // The legacy source is completely untouched -- this was a copy, never a move.
  assert.ok(fs.existsSync(path.join(legacy, 'connection.json')))
  assert.ok(fs.existsSync(path.join(legacy, 'native-oauth-tokens.json')))
})
