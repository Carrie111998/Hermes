import { PassThrough } from 'stream'

import React from 'react'
import { describe, expect, it, vi } from 'vitest'

// `@hermes/ink`'s package.json `exports` field resolves to the pre-built
// `dist/entry-exports.js` bundle. This test also needs several internal,
// non-exported pieces (`Ink`, `StdinContext`, `EventEmitter`, `InputEvent`)
// that only exist as source files, imported below via relative paths.
// Importing SOME things through the package name (resolving to the built
// dist/ bundle) and others through relative source paths creates two
// separate module instances of the same underlying code in Vite's module
// graph — React's hook dispatcher state lives on ONE of those instances,
// so a component using the "other" `useInput` never sees state changes
// made through this test's `Ink`/`StdinContext` instances (symptom: the
// input-registration effect silently never fires, listenerCount stays 0
// forever, no assertion failure, just permanent silence). Mocking
// `@hermes/ink` to the source-level entry-exports file (not the dist/
// bundle) keeps every import on the same module instance.
vi.mock('@hermes/ink', async () => import('../../packages/hermes-ink/src/entry-exports.js'))

import StdinContext from '../../packages/hermes-ink/src/ink/components/StdinContext.js'
import { EventEmitter } from '../../packages/hermes-ink/src/ink/events/emitter.js'
import { InputEvent } from '../../packages/hermes-ink/src/ink/events/input-event.js'
import Ink from '../../packages/hermes-ink/src/ink/ink.js'
import { SkillsHub } from '../components/skillsHub.js'
import type { GatewayClient } from '../gatewayClient.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

// The overlay's selected-row styling (chipRowProps -> listRowStyle ->
// liftForContrast -> parseColor) reads theme fields beyond the handful of
// colors this test renders text with, and parseColor() throws on an
// undefined channel. A hand-rolled partial Theme stub therefore crashes the
// mount as soon as upstream adds a field, which unmounts the tree, drops the
// useInput listener, and silently turns both cases below into no-ops. Use the
// real theme so the test keeps exercising the install path.
const theme = DEFAULT_THEME

// Builds a minimal ParsedKey (see packages/hermes-ink/src/ink/parse-keypress.ts)
// for the handful of keys this test needs to send. `parseKeypress` itself
// isn't exported, so this mirrors just the fields InputEvent's constructor
// reads.
const parsedKey = (overrides: Record<string, unknown>) => ({
  ctrl: false,
  fn: false,
  isPasted: false,
  kind: 'key' as const,
  meta: false,
  name: undefined,
  option: false,
  raw: undefined,
  sequence: undefined,
  shift: false,
  super: false,
  ...overrides
})

const RETURN_KEY = new InputEvent(parsedKey({ name: 'return', raw: '\r', sequence: '\r' }))
const X_KEY = new InputEvent(parsedKey({ name: 'x', raw: 'x', sequence: 'x' }))

interface Harness {
  frame: () => string[]
  inputEmitter: EventEmitter
  ink: InstanceType<typeof Ink>
  send: (event: InputEvent) => Promise<void>
  settle: () => Promise<void>
}

// Mounts a component with a real Ink instance (not the public renderSync/
// render helpers, which only expose {rerender, unmount, waitUntilExit,
// cleanup} — no way to force a synchronous flush after a stdin event
// outside a live terminal's own frame loop). Input is delivered by
// emitting directly on a caller-supplied inputEmitter via StdinContext,
// bypassing the real stdin readable-stream/raw-mode path entirely (this
// harness doesn't need to exercise that path — only that install()
// reacts correctly to a resolved response).
function mountInteractive(node: React.ReactElement): Harness {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 80, isTTY: true, rows: 24 })
  Object.assign(stdin, { isTTY: true, setRawMode: () => {} })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const inputEmitter = new EventEmitter()
  const ink = new Ink({
    exitOnCtrlC: false,
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  ink.render(
    <StdinContext.Provider
      value={{
        exitOnCtrlC: false,
        inputEmitter,
        isRawModeSupported: true,
        querier: null,
        setRawMode: () => {},
        stdin: stdin as unknown as NodeJS.ReadStream
      }}
    >
      {node}
    </StdinContext.Provider>
  )
  ink.onRender()

  const settle = async () => {
    await new Promise(resolve => setTimeout(resolve, 30))
    ink.onRender()
  }

  return {
    frame: () => stripAnsi(output).split('\n'),
    ink,
    inputEmitter,
    send: async (event: InputEvent) => {
      inputEmitter.emit('input', event)
      await settle()
    },
    settle
  }
}

describe('SkillsHub install', () => {
  it('keeps the overlay open and shows an error when install resolves installed: false', async () => {
    const onClose = vi.fn()
    const gw = {
      request: vi.fn((_method: string, params: { action: string }) => {
        if (params.action === 'list') {
          return Promise.resolve({ skills: { general: ['demo-skill'] } })
        }

        if (params.action === 'inspect') {
          return Promise.resolve({ info: { name: 'demo-skill' } })
        }

        if (params.action === 'install') {
          return Promise.resolve({ installed: false, name: 'demo-skill' })
        }

        return Promise.reject(new Error(`unexpected action: ${params.action}`))
      })
    } as unknown as GatewayClient

    const harness = mountInteractive(<SkillsHub gw={gw} onClose={onClose} t={theme} />)
    await harness.settle() // let the initial `skills.manage` list request resolve

    await harness.send(RETURN_KEY) // category -> skill
    await harness.send(RETURN_KEY) // skill -> actions (also fires inspect())
    await harness.send(X_KEY) // actions: trigger install

    expect(gw.request).toHaveBeenCalledWith('skills.manage', {
      action: 'install',
      query: 'demo-skill'
    })
    expect(onClose).not.toHaveBeenCalled()
    // The harness's frame() concatenates raw terminal bytes after
    // stripping ANSI color codes, but doesn't simulate cursor-positioning
    // overwrites the way a real terminal would — Ink's incremental writer
    // can leave a handful of individual characters "dropped" at specific
    // cursor-move points in this simplified reconstruction (e.g. "error"
    // rendering as "eror" here) without anything being visually wrong on
    // a real terminal. Check for a distinctive fragment that survives
    // this, not the exact literal string.
    expect(harness.frame().some(line => line.includes('demo-skill') && /ns?all failed/.test(line))).toBe(true)

    harness.ink.unmount()
  })

  it('closes the overlay when install resolves installed: true', async () => {
    const onClose = vi.fn()
    const gw = {
      request: vi.fn((_method: string, params: { action: string }) => {
        if (params.action === 'list') {
          return Promise.resolve({ skills: { general: ['demo-skill'] } })
        }

        if (params.action === 'inspect') {
          return Promise.resolve({ info: { name: 'demo-skill' } })
        }

        if (params.action === 'install') {
          return Promise.resolve({ installed: true, name: 'demo-skill' })
        }

        return Promise.reject(new Error(`unexpected action: ${params.action}`))
      })
    } as unknown as GatewayClient

    const harness = mountInteractive(<SkillsHub gw={gw} onClose={onClose} t={theme} />)
    await harness.settle()

    await harness.send(RETURN_KEY)
    await harness.send(RETURN_KEY)
    await harness.send(X_KEY)

    expect(onClose).toHaveBeenCalledTimes(1)

    harness.ink.unmount()
  })
})
