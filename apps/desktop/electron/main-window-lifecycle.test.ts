import assert from 'node:assert/strict'

import { test } from 'vitest'

import { ensureMainWindow, focusMainWindow, hideMainWindow } from './main-window-lifecycle'

test('hides only a live visible primary window', () => {
  const calls: string[] = []

  const visibleWindow = {
    hide: () => calls.push('hide'),
    isDestroyed: () => false,
    isVisible: () => true
  }

  assert.equal(hideMainWindow(visibleWindow), true)
  assert.deepEqual(calls, ['hide'])

  assert.equal(
    hideMainWindow({
      hide: () => assert.fail('an already hidden window must not be hidden again'),
      isDestroyed: () => false,
      isVisible: () => false
    }),
    false
  )
  assert.equal(hideMainWindow(null), false)
})

test('reveals a tray-hidden primary window before focusing it', () => {
  const calls: string[] = []

  const hiddenWindow = {
    focus: () => calls.push('focus'),
    isDestroyed: () => false,
    isMinimized: () => false,
    isVisible: () => false,
    restore: () => calls.push('restore'),
    show: () => calls.push('show')
  }

  focusMainWindow(hiddenWindow)

  assert.deepEqual(calls, ['show', 'focus'])
})

test('restores a minimized primary window before focusing it', () => {
  const calls: string[] = []

  const minimizedWindow = {
    focus: () => calls.push('focus'),
    isDestroyed: () => false,
    isMinimized: () => true,
    isVisible: () => true,
    restore: () => calls.push('restore'),
    show: () => calls.push('show')
  }

  focusMainWindow(minimizedWindow)

  assert.deepEqual(calls, ['restore', 'focus'])
})

test('recreates a destroyed primary window without focusing it', () => {
  const destroyedWindow = {
    isDestroyed: () => true
  }

  let createCalls = 0
  let focusCalls = 0

  ensureMainWindow(destroyedWindow, {
    isReady: true,
    createWindow: () => {
      createCalls += 1
    },
    focusWindow: () => {
      focusCalls += 1
    }
  })

  assert.equal(createCalls, 1)
  assert.equal(focusCalls, 0)
})

test('waits for app readiness before recreating a primary window', () => {
  let createCalls = 0

  ensureMainWindow(null, {
    isReady: false,
    createWindow: () => {
      createCalls += 1
    },
    focusWindow: () => assert.fail('missing window must not be focused')
  })

  assert.equal(createCalls, 0)
})

test('focuses a live primary window for a normal second launch', () => {
  const liveWindow = {
    isDestroyed: () => false
  }

  let focusedWindow = null

  ensureMainWindow(liveWindow, {
    isReady: true,
    createWindow: () => assert.fail('live window must not be replaced'),
    focusWindow: window => {
      focusedWindow = window
    }
  })

  assert.equal(focusedWindow, liveWindow)
})

test('leaves live-window focus to deep-link delivery', () => {
  const liveWindow = {
    isDestroyed: () => false
  }

  ensureMainWindow(liveWindow, {
    isReady: true,
    createWindow: () => assert.fail('live window must not be replaced'),
    focusWindow: () => assert.fail('deep-link delivery owns focus'),
    focusExisting: false
  })
})
