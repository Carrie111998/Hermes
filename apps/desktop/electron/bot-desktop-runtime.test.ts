import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import {
  botDesktopWorkspacePath,
  buildBotDesktopWindowUrl,
  createBotDesktopWindowRegistry,
  normalizeBotDesktopProfile
} from './bot-desktop-runtime'

function makeFakeWindow() {
  const listeners: Record<string, () => void> = {}
  const calls = { close: 0, focus: 0, restore: 0, show: 0 }
  let destroyed = false
  let minimized = false
  let visible = true

  return {
    on(event: string, listener: () => void) {
      listeners[event] = listener
    },
    emit(event: string) {
      listeners[event]?.()
    },
    isDestroyed: () => destroyed,
    isMinimized: () => minimized,
    isVisible: () => visible,
    restore() {
      calls.restore += 1
      minimized = false
    },
    show() {
      calls.show += 1
      visible = true
    },
    focus() {
      calls.focus += 1
    },
    close() {
      calls.close += 1
      destroyed = true
    },
    setMinimized(value: boolean) {
      minimized = value
    },
    setVisible(value: boolean) {
      visible = value
    },
    calls
  }
}

test('normalizes profile identity and rejects unsafe desktop keys', () => {
  assert.equal(normalizeBotDesktopProfile('  analyst_1 '), 'analyst_1')
  assert.equal(normalizeBotDesktopProfile(''), 'default')
  assert.throws(() => normalizeBotDesktopProfile('../shared'), /must match/)
  assert.throws(() => normalizeBotDesktopProfile('Bot Name'), /must match/)
})

test('keeps persistent workspaces distinct for each Bot', () => {
  assert.equal(
    botDesktopWorkspacePath('C:/Hermes', 'alpha'),
    path.join(path.resolve('C:/Hermes'), 'profiles', 'alpha', 'desktop-workspace')
  )
  assert.equal(
    botDesktopWorkspacePath('C:/Hermes', 'default'),
    path.join(path.resolve('C:/Hermes'), 'desktop-workspace')
  )
})

test('builds a specialized renderer URL with a profile query', () => {
  assert.equal(
    buildBotDesktopWindowUrl('alpha', { devServer: 'http://127.0.0.1:5174/' }),
    'http://127.0.0.1:5174/?profile=alpha&win=bot-desktop'
  )
})

test('registry reuses one standalone window per Bot profile', () => {
  const registry = createBotDesktopWindowRegistry()
  const alpha = makeFakeWindow()
  let builds = 0

  const first = registry.openOrFocus('alpha', () => {
    builds += 1

    return alpha
  })

  const second = registry.openOrFocus('alpha', () => {
    builds += 1

    return makeFakeWindow()
  })

  assert.equal(first, second)
  assert.equal(builds, 1)
  assert.equal(alpha.calls.focus, 1)
  assert.equal(registry.size, 1)

  alpha.emit('closed')
  assert.equal(registry.size, 0)
})
