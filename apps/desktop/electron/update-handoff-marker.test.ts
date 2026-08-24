import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..')
const POSIX_SCRIPT = path.join(REPO_ROOT, 'scripts', 'desktop-update', 'posix.sh')
const WINDOWS_SCRIPT = path.join(REPO_ROOT, 'scripts', 'desktop-update', 'windows.ps1')

function sandbox(tag: string) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-handoff-marker-${tag}-`))
  const installRoot = path.join(home, 'hermes-agent')
  fs.mkdirSync(installRoot)

  return { home, installRoot }
}

function markerStartedAt(home: string): number {
  const [, startedAt] = fs.readFileSync(path.join(home, '.hermes-update-in-progress'), 'utf8').split('\n')

  return Number.parseInt(startedAt, 10)
}

interface HandoffRunOptions {
  startedAt?: string
  desktopPid?: number
  invocationSentinel?: string
}

function handoffEnv(options: HandoffRunOptions) {
  const env = { ...process.env }

  if (options.startedAt === undefined) {
    delete env.HERMES_UPDATE_STARTED_AT
  } else {
    env.HERMES_UPDATE_STARTED_AT = options.startedAt
  }
  if (options.invocationSentinel === undefined) {
    delete env.HERMES_UPDATE_SELFTEST_INVOCATION_SENTINEL
  } else {
    env.HERMES_UPDATE_SELFTEST_INVOCATION_SENTINEL = options.invocationSentinel
  }

  return env
}

function runPosix(installRoot: string, options: HandoffRunOptions = {}) {
  return spawnSync(
    '/bin/bash',
    [
      POSIX_SCRIPT,
      '--daemonized',
      '--install-root',
      installRoot,
      '--desktop-pid',
      String(options.desktopPid || 0),
      '--self-test-marker'
    ],
    {
      env: handoffEnv(options),
      encoding: 'utf8'
    }
  )
}

function runWindows(installRoot: string, options: HandoffRunOptions = {}) {
  return spawnSync(
    'powershell.exe',
    [
      '-NoProfile',
      '-ExecutionPolicy',
      'Bypass',
      '-File',
      WINDOWS_SCRIPT,
      '-InstallRoot',
      installRoot,
      '-DesktopPid',
      String(options.desktopPid || 0),
      '-NoUi',
      '-NoMarkerCleanup',
      '-SelfTestMarker'
    ],
    { env: handoffEnv(options), encoding: 'utf8' }
  )
}

function assertScriptHandoff(run: (installRoot: string, options?: HandoffRunOptions) => ReturnType<typeof spawnSync>) {
  const preserved = sandbox('preserved')
  const acquiredAt = Math.floor(Date.now() / 1000) - 300
  const preservedResult = run(preserved.installRoot, { startedAt: String(acquiredAt) })

  assert.equal(preservedResult.status, 0, String(preservedResult.stderr || preservedResult.stdout))
  assert.equal(markerStartedAt(preserved.home), acquiredAt, 'the script must preserve the Desktop acquisition time')

  const bridge = sandbox('bridge')
  fs.writeFileSync(path.join(bridge.home, '.hermes-update-in-progress'), `${process.pid}\n${acquiredAt}\n`)
  const bridgeResult = run(bridge.installRoot, {
    startedAt: String(acquiredAt),
    desktopPid: process.pid
  })

  assert.equal(bridgeResult.status, 0, String(bridgeResult.stderr || bridgeResult.stdout))
  const [bridgeOwner, bridgeStartedAt] = fs
    .readFileSync(path.join(bridge.home, '.hermes-update-in-progress'), 'utf8')
    .trim()
    .split('\n')
    .map(Number)
  assert.notEqual(bridgeOwner, process.pid, 'the handoff script must take ownership from its authorized Desktop')
  assert.equal(bridgeStartedAt, acquiredAt, 'authorized takeover must preserve the Desktop acquisition time')

  const refreshed = sandbox('refreshed')
  fs.writeFileSync(path.join(refreshed.home, '.hermes-update-in-progress'), '999999\n1\n')
  const before = Math.floor(Date.now() / 1000)
  const refreshedResult = run(refreshed.installRoot, { startedAt: 'malformed' })
  const after = Math.floor(Date.now() / 1000)

  assert.equal(refreshedResult.status, 0, String(refreshedResult.stderr || refreshedResult.stdout))
  assert.ok(
    markerStartedAt(refreshed.home) >= before && markerStartedAt(refreshed.home) <= after,
    'an invalid hand-off timestamp must start a fresh claim'
  )

  const oversized = sandbox('oversized')
  const oversizedBefore = Math.floor(Date.now() / 1000)
  const oversizedResult = run(oversized.installRoot, { startedAt: '99999999999999999999' })
  const oversizedAfter = Math.floor(Date.now() / 1000)

  assert.equal(oversizedResult.status, 0, String(oversizedResult.stderr || oversizedResult.stdout))
  assert.ok(
    markerStartedAt(oversized.home) >= oversizedBefore && markerStartedAt(oversized.home) <= oversizedAfter,
    'an oversized hand-off timestamp must start a fresh claim'
  )

  const foreign = sandbox('live-foreign')
  const foreignOwner = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' })
  assert.ok(foreignOwner.pid)
  const foreignRaw = `${foreignOwner.pid}\n${acquiredAt}\n`
  const foreignMarker = path.join(foreign.home, '.hermes-update-in-progress')
  const invocationSentinel = path.join(foreign.home, 'update-invocation-reached')
  fs.writeFileSync(foreignMarker, foreignRaw)
  try {
    const foreignResult = run(foreign.installRoot, {
      startedAt: String(acquiredAt),
      invocationSentinel
    })

    assert.notEqual(foreignResult.status, 0, 'a live foreign update owner must make the handoff refuse')
    assert.equal(fs.readFileSync(foreignMarker, 'utf8'), foreignRaw, 'the foreign marker must remain byte-identical')
    assert.equal(
      fs.existsSync(invocationSentinel),
      false,
      'a refused handoff must not reach the update invocation boundary'
    )
  } finally {
    foreignOwner.kill()
  }
}

test.skipIf(process.platform === 'win32')(
  'POSIX hand-off claims only its Desktop bridge and preserves a live foreign owner',
  () => {
    assertScriptHandoff(runPosix)
  }
)

test.skipIf(process.platform !== 'win32')(
  'PowerShell hand-off claims only its Desktop bridge and preserves a live foreign owner',
  () => {
    assertScriptHandoff(runWindows)
  }
)
