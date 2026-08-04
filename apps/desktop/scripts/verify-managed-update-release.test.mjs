import assert from 'node:assert/strict'
import crypto from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'
import zlib from 'node:zlib'

import { refreshAndVerifyManagedUpdateRelease } from './refresh-managed-update-metadata.mjs'
import {
  parseManagedUpdateInfo,
  renderArtifactName,
  verifyManagedUpdateRelease
} from './verify-managed-update-release.mjs'

const manifest = {
  productName: 'evaOS Agent',
  version: '2026.7.20-es.11',
  build: {
    appId: 'com.electricsheephq.evaos.agent',
    artifactName: 'evaOS-Agent-${version}-${arch}.${ext}',
    executableName: 'evaOS Agent',
    productName: 'evaOS Agent'
  }
}

function digest(bytes) {
  return crypto.createHash('sha512').update(bytes).digest('base64')
}

function writeFakeApp(root) {
  const appPath = path.join(root, 'evaOS Agent.app')
  const contents = path.join(appPath, 'Contents')
  const macos = path.join(contents, 'MacOS')
  fs.mkdirSync(macos, { recursive: true })
  fs.writeFileSync(path.join(contents, 'Info.plist'), '<plist/>')
  fs.writeFileSync(path.join(macos, 'evaOS Agent'), 'arm64 executable')

  return appPath
}

function createReleaseFixture() {
  const releaseDir = fs.mkdtempSync(path.join(os.tmpdir(), 'evaos-release-verifier-test-'))
  const base = `evaOS-Agent-${manifest.version}-arm64`
  const content = {
    dmg: Buffer.from('signed notarized dmg'),
    dmgBlockmap: Buffer.from('dmg blockmap'),
    zip: Buffer.from('signed notarized zip'),
    zipBlockmap: Buffer.from('zip blockmap')
  }
  fs.writeFileSync(path.join(releaseDir, `${base}.dmg`), content.dmg)
  fs.writeFileSync(path.join(releaseDir, `${base}.dmg.blockmap`), content.dmgBlockmap)
  fs.writeFileSync(path.join(releaseDir, `${base}.zip`), content.zip)
  fs.writeFileSync(path.join(releaseDir, `${base}.zip.blockmap`), content.zipBlockmap)
  fs.writeFileSync(
    path.join(releaseDir, 'latest-mac.yml'),
    [
      `version: ${manifest.version}`,
      'files:',
      `  - url: ${base}.zip`,
      `    sha512: ${digest(content.zip)}`,
      `    size: ${content.zip.length}`,
      `  - url: ${base}.dmg`,
      `    sha512: ${digest(content.dmg)}`,
      `    size: ${content.dmg.length}`,
      `path: ${base}.zip`,
      `sha512: ${digest(content.zip)}`,
      "releaseDate: '2026-08-04T00:00:00.000Z'",
      ''
    ].join('\n')
  )

  return {
    cleanup: () => fs.rmSync(releaseDir, { force: true, recursive: true }),
    releaseDir
  }
}

function fakeMacCommands(calls) {
  return async (command, args) => {
    calls.push({ args, command })

    if (command === 'ditto') {
      writeFakeApp(args.at(-1))
    }
    if (command === 'hdiutil' && args[0] === 'attach') {
      writeFakeApp(args[args.indexOf('-mountpoint') + 1])
    }
    if (command === 'plutil') {
      const values = {
        CFBundleDisplayName: 'evaOS Agent',
        CFBundleExecutable: 'evaOS Agent',
        CFBundleIdentifier: 'com.electricsheephq.evaos.agent',
        CFBundleShortVersionString: manifest.version
      }

      return { stderr: '', stdout: `${values[args[1]]}\n` }
    }
    if (command === 'lipo') {
      return { stderr: '', stdout: 'arm64\n' }
    }
    if (command === 'codesign' && args.includes('--display')) {
      return {
        stderr:
          'Authority=Developer ID Application: Electric Sheep (ABCDE12345)\n' +
          'TeamIdentifier=ABCDE12345\nflags=0x10000(runtime)\n',
        stdout: ''
      }
    }
    if (command === 'spctl') {
      return { stderr: 'accepted\nsource=Notarized Developer ID\n', stdout: '' }
    }

    return { stderr: '', stdout: '' }
  }
}

test('parses electron-builder mac update metadata without reading release notes as files', () => {
  const parsed = parseManagedUpdateInfo(
    [
      'version: 1.2.3',
      'files:',
      '  - url: app.zip',
      '    sha512: abc',
      '    size: 12',
      'path: app.zip',
      'sha512: abc',
      'releaseNotes: |',
      '  - url: not-an-asset'
    ].join('\n')
  )

  assert.deepEqual(parsed, {
    files: [{ sha512: 'abc', size: 12, url: 'app.zip' }],
    path: 'app.zip',
    sha512: 'abc',
    version: '1.2.3'
  })
})

test('derives release filenames from package metadata', () => {
  assert.equal(
    renderArtifactName(manifest, { arch: 'arm64', ext: 'dmg', version: manifest.version }),
    'evaOS-Agent-2026.7.20-es.11-arm64.dmg'
  )
})

test('verifies appcast bytes and both notarized arm64 app payloads', async () => {
  const fixture = createReleaseFixture()
  const calls = []

  try {
    const result = await verifyManagedUpdateRelease({
      manifest,
      platform: 'darwin',
      releaseDir: fixture.releaseDir,
      runCommand: fakeMacCommands(calls)
    })

    assert.equal(result.schema_version, 'evaos.agent_update_release.v2')
    assert.equal(result.version, manifest.version)
    assert.equal(result.arch, 'arm64')
    assert.equal(result.assets.length, 5)
    assert.deepEqual(result.packaged_apps.zip.architectures, ['arm64'])
    assert.deepEqual(result.packaged_apps.dmg.architectures, ['arm64'])
    assert.equal(calls.filter(call => call.command === 'codesign' && call.args.includes('--verify')).length, 2)
    assert.equal(calls.filter(call => call.command === 'spctl').length, 2)
    assert.equal(calls.filter(call => call.command === 'xcrun').length, 2)
    assert.equal(
      calls.some(call => call.command === 'hdiutil' && call.args[0] === 'detach'),
      true
    )
  } finally {
    fixture.cleanup()
  }
})

test('fails before extraction when the appcast SHA-512 does not match the ZIP', async () => {
  const fixture = createReleaseFixture()
  const updateInfoPath = path.join(fixture.releaseDir, 'latest-mac.yml')
  fs.writeFileSync(
    updateInfoPath,
    fs.readFileSync(updateInfoPath, 'utf8').replace(digest(Buffer.from('signed notarized zip')), 'tampered')
  )

  try {
    await assert.rejects(
      verifyManagedUpdateRelease({
        manifest,
        platform: 'darwin',
        releaseDir: fixture.releaseDir,
        runCommand: async () => {
          throw new Error('artifact extraction must not run')
        }
      }),
      /SHA-512 does not match/
    )
  } finally {
    fixture.cleanup()
  }
})

test('rejects a stapled DMG while its pre-staple update metadata is stale', async () => {
  const fixture = createReleaseFixture()
  const dmgPath = path.join(fixture.releaseDir, `evaOS-Agent-${manifest.version}-arm64.dmg`)
  fs.appendFileSync(dmgPath, ' stapled ticket')

  try {
    await assert.rejects(
      verifyManagedUpdateRelease({
        manifest,
        platform: 'darwin',
        releaseDir: fixture.releaseDir,
        runCommand: async () => {
          throw new Error('artifact extraction must not run')
        }
      }),
      /size does not match .*\.dmg/
    )
  } finally {
    fixture.cleanup()
  }
})

test('regenerates the stapled DMG blockmap and metadata without changing ZIP identity', async () => {
  const fixture = createReleaseFixture()
  const base = `evaOS-Agent-${manifest.version}-arm64`
  const dmgPath = path.join(fixture.releaseDir, `${base}.dmg`)
  const blockmapPath = path.join(fixture.releaseDir, `${base}.dmg.blockmap`)
  const updateInfoPath = path.join(fixture.releaseDir, 'latest-mac.yml')
  const originalInfo = parseManagedUpdateInfo(fs.readFileSync(updateInfoPath, 'utf8'))
  const originalZip = originalInfo.files.find(file => file.url === `${base}.zip`)
  fs.appendFileSync(dmgPath, ' stapled ticket')

  try {
    const result = await refreshAndVerifyManagedUpdateRelease({
      manifest,
      platform: 'darwin',
      releaseDir: fixture.releaseDir,
      runCommand: fakeMacCommands([])
    })
    const refreshedInfo = parseManagedUpdateInfo(fs.readFileSync(updateInfoPath, 'utf8'))
    const refreshedZip = refreshedInfo.files.find(file => file.url === `${base}.zip`)
    const refreshedDmg = refreshedInfo.files.find(file => file.url === `${base}.dmg`)
    const firstBlockmap = fs.readFileSync(blockmapPath)
    const firstUpdateInfo = fs.readFileSync(updateInfoPath, 'utf8')
    const decodedBlockmap = JSON.parse(zlib.gunzipSync(firstBlockmap).toString('utf8'))

    assert.deepEqual(refreshedZip, originalZip)
    assert.equal(refreshedInfo.path, originalInfo.path)
    assert.equal(refreshedInfo.sha512, originalInfo.sha512)
    assert.equal(refreshedDmg.size, fs.statSync(dmgPath).size)
    assert.equal(refreshedDmg.sha512, digest(fs.readFileSync(dmgPath)))
    assert.equal(result.refreshed.size, refreshedDmg.size)
    assert.equal(decodedBlockmap.files[0].sizes.reduce((total, size) => total + size, 0), refreshedDmg.size)
    assert.equal(result.verified.version, manifest.version)

    await refreshAndVerifyManagedUpdateRelease({
      manifest,
      platform: 'darwin',
      releaseDir: fixture.releaseDir,
      runCommand: fakeMacCommands([])
    })
    assert.deepEqual(fs.readFileSync(blockmapPath), firstBlockmap)
    assert.equal(fs.readFileSync(updateInfoPath, 'utf8'), firstUpdateInfo)
  } finally {
    fixture.cleanup()
  }
})

test('fails closed when release verification is attempted off macOS', async () => {
  await assert.rejects(
    verifyManagedUpdateRelease({ manifest, platform: 'linux', releaseDir: '/tmp/unused' }),
    /must run on macOS/
  )
})
