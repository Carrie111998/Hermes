import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import type { App, WebContents } from 'electron'
import { test, vi } from 'vitest'

import {
  E2E_RENDERER_STARTUP_ERRORS_PROPERTY,
  installE2ERendererStartupObserver
} from './e2e-renderer-startup-observer'

interface ObservedApp extends App {
  [E2E_RENDERER_STARTUP_ERRORS_PROPERTY]?: string[]
}

function createHarness(env: NodeJS.ProcessEnv = { HERMES_E2E_OBSERVE_RENDERER_STARTUP: '1' }) {
  const appEvents = new EventEmitter()
  const contentsEvents = new EventEmitter()

  const executeJavaScript = vi.fn(async (source: string) => {
    contentsEvents.emit('console-message', {}, {
      level: 3,
      lineNumber: 1,
      message: source.includes('HERMES_E2E_STARTUP_RENDERER_ERROR_SENTINEL')
        ? 'HERMES_E2E_STARTUP_RENDERER_ERROR_SENTINEL'
        : source,
      sourceUrl: 'e2e://startup-observer'
    })
  })

  const app = appEvents as unknown as App
  const contents = Object.assign(contentsEvents, { executeJavaScript }) as unknown as WebContents

  installE2ERendererStartupObserver(app, env)
  appEvents.emit('web-contents-created', {}, contents)

  return {
    contentsEvents,
    errors: (app as ObservedApp)[E2E_RENDERER_STARTUP_ERRORS_PROPERTY] ?? [],
    executeJavaScript
  }
}

test('captures the Electron 36+ console-message details shape', () => {
  const harness = createHarness()

  harness.contentsEvents.emit('console-message', {}, {
    level: 3,
    lineNumber: 7,
    message: 'modern startup failure',
    sourceUrl: 'e2e://renderer'
  })

  assert.deepEqual(harness.errors, ['console: modern startup failure'])
})

test('injects the E2E sentinel through the real renderer console event path', async () => {
  const harness = createHarness({
    HERMES_E2E_INJECT_STARTUP_RENDERER_ERROR: '1',
    HERMES_E2E_OBSERVE_RENDERER_STARTUP: '1'
  })

  assert.deepEqual(harness.errors, [])
  harness.contentsEvents.emit('dom-ready')
  await Promise.resolve()

  assert.equal(harness.executeJavaScript.mock.calls.length, 1)
  assert.deepEqual(harness.errors, ['console: HERMES_E2E_STARTUP_RENDERER_ERROR_SENTINEL'])
})
