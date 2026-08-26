import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  shouldCreateWindowsTray,
  shouldHideMainWindowOnClose,
  shouldStartMainWindowHidden,
  shouldTreatSessionEndAsFinalQuit
} from './windows-tray-lifecycle'

test('Windows close-to-tray lifecycle policy preserves explicit quit and non-Windows behavior', () => {
  assert.equal(shouldCreateWindowsTray('win32'), true)
  assert.equal(shouldCreateWindowsTray('darwin'), false)
  assert.equal(shouldTreatSessionEndAsFinalQuit('win32'), true)
  assert.equal(shouldTreatSessionEndAsFinalQuit('darwin'), false)

  assert.equal(shouldHideMainWindowOnClose({ platform: 'win32', isQuitting: false, trayAvailable: true }), true)
  assert.equal(shouldHideMainWindowOnClose({ platform: 'win32', isQuitting: false, trayAvailable: false }), false)
  assert.equal(shouldHideMainWindowOnClose({ platform: 'win32', isQuitting: true, trayAvailable: true }), false)
  assert.equal(shouldHideMainWindowOnClose({ platform: 'linux', isQuitting: false, trayAvailable: true }), false)

  assert.equal(
    shouldStartMainWindowHidden({ platform: 'win32', argv: ['Hermes.exe', '--hidden'], trayAvailable: true }),
    true
  )
  assert.equal(
    shouldStartMainWindowHidden({ platform: 'win32', argv: ['Hermes.exe', '--hidden'], trayAvailable: false }),
    false
  )
  assert.equal(shouldStartMainWindowHidden({ platform: 'win32', argv: ['Hermes.exe'], trayAvailable: true }), false)
  assert.equal(shouldStartMainWindowHidden({ platform: 'darwin', argv: ['Hermes', '--hidden'], trayAvailable: true }), false)
})
