import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  shouldCreateWindowsTray,
  shouldHideMainWindowOnClose,
  shouldStartMainWindowHidden
} from './windows-tray-lifecycle'

test('Windows close-to-tray lifecycle policy preserves explicit quit and non-Windows behavior', () => {
  assert.equal(shouldCreateWindowsTray('win32'), true)
  assert.equal(shouldCreateWindowsTray('darwin'), false)

  assert.equal(shouldHideMainWindowOnClose({ platform: 'win32', isQuitting: false }), true)
  assert.equal(shouldHideMainWindowOnClose({ platform: 'win32', isQuitting: true }), false)
  assert.equal(shouldHideMainWindowOnClose({ platform: 'linux', isQuitting: false }), false)

  assert.equal(shouldStartMainWindowHidden({ platform: 'win32', argv: ['Hermes.exe', '--hidden'] }), true)
  assert.equal(shouldStartMainWindowHidden({ platform: 'win32', argv: ['Hermes.exe'] }), false)
  assert.equal(shouldStartMainWindowHidden({ platform: 'darwin', argv: ['Hermes', '--hidden'] }), false)
})
