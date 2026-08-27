import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  guardRendererBootAcrossUpdate,
  shouldQuiesceForExternalUpdate
} from './renderer-update-overlap'
import { markerPath, readLiveUpdateMarker, writeUpdateMarker } from './update-marker'

test('renderer boot continues immediately when no update owns the install', async () => {
  let ticks = 0

  const action = await guardRendererBootAcrossUpdate(
    { hasLiveMarker: () => false, isUpdateInFlight: () => false },
    {
      onWaitTick: () => {
        ticks += 1
      },
      pollMs: 1,
      sleep: async () => {},
      timeoutMs: 100
    }
  )

  assert.equal(action, 'continue')
  assert.equal(ticks, 0)
})

test('renderer boot relaunches after an overlapping marker clears', async () => {
  let marker = true
  let ticks = 0

  const action = await guardRendererBootAcrossUpdate(
    { hasLiveMarker: () => marker, isUpdateInFlight: () => false },
    {
      onWaitTick: reason => {
        ticks += 1
        assert.equal(reason, 'marker')

        if (ticks === 2) {
          marker = false
        }
      },
      pollMs: 1,
      sleep: async () => {},
      timeoutMs: 100
    }
  )

  assert.equal(action, 'relaunch')
  assert.equal(ticks, 2)
})

test('renderer boot also relaunches after an in-process update clears', async () => {
  let inFlight = true

  const action = await guardRendererBootAcrossUpdate(
    { hasLiveMarker: () => false, isUpdateInFlight: () => inFlight },
    {
      onWaitTick: () => {
        inFlight = false
      },
      pollMs: 1,
      sleep: async () => {},
      timeoutMs: 100
    }
  )

  assert.equal(action, 'relaunch')
})

test('renderer boot aborts instead of loading files while a live update times out', async () => {
  let clock = 0

  const action = await guardRendererBootAcrossUpdate(
    { hasLiveMarker: () => true, isUpdateInFlight: () => false },
    {
      now: () => clock,
      pollMs: 10,
      sleep: async ms => {
        clock += ms
      },
      timeoutMs: 30
    }
  )

  assert.equal(action, 'abort')
})

test('real update marker integration parks, observes clearance, and requires relaunch', async () => {
  const hermesHome = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-renderer-update-overlap-'))

  try {
    writeUpdateMarker(hermesHome, process.pid)
    assert.ok(readLiveUpdateMarker(hermesHome))

    let ticks = 0

    const action = await guardRendererBootAcrossUpdate(
      {
        hasLiveMarker: () => Boolean(readLiveUpdateMarker(hermesHome)),
        isUpdateInFlight: () => false
      },
      {
        onWaitTick: () => {
          ticks += 1

          if (ticks === 2) {
            fs.unlinkSync(markerPath(hermesHome))
          }
        },
        pollMs: 1,
        sleep: async () => {},
        timeoutMs: 100
      }
    )

    assert.equal(action, 'relaunch')
    assert.equal(ticks, 2)
  } finally {
    fs.rmSync(hermesHome, { recursive: true, force: true })
  }
})

test('live external markers quiesce a renderer exactly once', () => {
  assert.equal(
    shouldQuiesceForExternalUpdate({
      alreadyQuiescing: false,
      isQuittingForHandoff: false,
      markerLive: true,
      updateInFlight: false
    }),
    true
  )

  for (const state of [
    { alreadyQuiescing: true, isQuittingForHandoff: false, markerLive: true, updateInFlight: false },
    { alreadyQuiescing: false, isQuittingForHandoff: true, markerLive: true, updateInFlight: false },
    { alreadyQuiescing: false, isQuittingForHandoff: false, markerLive: true, updateInFlight: true },
    { alreadyQuiescing: false, isQuittingForHandoff: false, markerLive: false, updateInFlight: false }
  ]) {
    assert.equal(shouldQuiesceForExternalUpdate(state), false)
  }
})

test('main waits before creating a renderer and suppresses second-instance windows while parked', () => {
  const mainSource = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8').replace(/\r\n/g, '\n')
  const readyStart = mainSource.indexOf('app.whenReady().then(')
  const createWindow = mainSource.indexOf('\n  createWindow()', readyStart)
  const guard = mainSource.indexOf('await guardRendererBootForUpdateOverlap()', readyStart)
  const suppressSecondInstance = mainSource.indexOf('if (rendererBootHeldForUpdate)', mainSource.indexOf("app.on('second-instance'"))
  const suppressDockActivation = mainSource.indexOf('if (rendererBootHeldForUpdate)', mainSource.indexOf("app.on('activate'"))

  assert.ok(readyStart >= 0, 'whenReady boot block must exist')
  assert.ok(guard > readyStart, 'update-overlap guard must run during ready boot')
  assert.ok(createWindow > guard, 'normal renderer creation must happen only after the update-overlap guard')
  assert.ok(suppressSecondInstance >= 0, 'second-instance launches must not bypass the renderer boot hold')
  assert.ok(suppressDockActivation >= 0, 'Dock activation must not recreate a renderer during the boot hold')
})

test('main monitors for external CLI updates after the renderer starts', () => {
  const mainSource = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8').replace(/\r\n/g, '\n')
  const createWindow = mainSource.indexOf('\n  createWindow()', mainSource.indexOf('app.whenReady().then('))
  const monitor = mainSource.indexOf('startExternalUpdateRendererMonitor()', createWindow)
  const quiesceFunction = mainSource.indexOf('async function quiesceRendererForBundleUpdate(')
  const quiesceEnd = mainSource.indexOf('function startExternalUpdateRendererMonitor()', quiesceFunction)
  const quiesceSource = mainSource.slice(quiesceFunction, quiesceEnd)

  assert.ok(createWindow >= 0)
  assert.ok(monitor > createWindow, 'external update monitor must be installed after normal renderer startup')
  assert.ok(quiesceFunction >= 0, 'live renderers need one orderly update-quiesce path')
  assert.match(quiesceSource, /app\.relaunch\(\)[\s\S]*?BrowserWindow\.getAllWindows\(\)[\s\S]*?win\.destroy\(\)/)
})

test('a backend wait that observes an overlapping update cannot resume the old renderer generation', () => {
  const mainSource = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8').replace(/\r\n/g, '\n')
  const waitStart = mainSource.indexOf('async function waitForUpdateToFinish()')
  const waitEnd = mainSource.indexOf('\nfunction unpackedPathFor(', waitStart)
  const waitSource = mainSource.slice(waitStart, waitEnd)

  assert.ok(waitStart >= 0)
  assert.match(waitSource, /outcome === 'timeout'[\s\S]*?await quiesceRendererForBundleUpdate\(\{ relaunch: false \}\)/)
  assert.match(waitSource, /outcome === 'finished'[\s\S]*?await quiesceRendererForBundleUpdate\(\{ relaunch: true \}\)/)
  assert.doesNotMatch(waitSource, /starting backend anyway|proceeding with backend start/)
})
