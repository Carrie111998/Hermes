import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test } from 'vitest'

import { openWslExternalUrl, wslWindowsOpenUrlArgs } from './wsl-open-url'

const SHELL_COMMANDS = new Set(['cmd.exe', 'cmd', 'command.com', 'powershell.exe', 'powershell', 'pwsh.exe', 'pwsh'])

const HTTPS_META = 'https://example.com/?a=1&calc&z=2|x^y<z>w%20q'
const MAILTO_META = 'mailto:user@example.com?subject=a&b|c^d<e>f%20g'

type SpawnCall = {
  command: string
  args: string[]
  options: { detached: boolean; stdio: 'ignore'; windowsHide: boolean }
}

function makeChild() {
  const child = new EventEmitter() as EventEmitter & { unrefCalls: number; unref: () => void }
  child.unrefCalls = 0
  child.unref = () => {
    child.unrefCalls += 1
  }

  return child
}

function launch(url: string) {
  const calls: SpawnCall[] = []
  const logs: string[] = []
  const fallbacks: string[] = []
  const child = makeChild()

  const opened = openWslExternalUrl(url, {
    spawn: (command, args, options) => {
      calls.push({ command, args, options })

      return child
    },
    fallback: openedUrl => {
      fallbacks.push(openedUrl)
    },
    log: message => {
      logs.push(message)
    }
  })

  return { calls, child, fallbacks, logs, opened }
}

function assertSafeLaunch(call: SpawnCall, url: string) {
  assert.equal(SHELL_COMMANDS.has(call.command.toLowerCase()), false)
  assert.notEqual(call.command.toLowerCase(), 'cmd.exe')
  assert.equal(call.args.includes('start'), false)
  assert.equal(call.args.includes('/c'), false)
  assert.equal(call.command, 'rundll32.exe')
  assert.equal(call.args.length, 2)
  assert.equal(call.args[0], 'url.dll,FileProtocolHandler')
  assert.equal(call.args[1], url)
  assert.deepEqual(call.options, { detached: true, stdio: 'ignore', windowsHide: true })
}

test('openWslExternalUrl launches rundll32 with the https URL as one argv element', () => {
  const { calls, child, fallbacks, opened } = launch(HTTPS_META)

  assert.equal(opened, true)
  assert.equal(calls.length, 1)
  assertSafeLaunch(calls[0], HTTPS_META)
  assert.equal(calls[0].args[1], HTTPS_META)
  assert.equal(child.unrefCalls, 1)
  child.emit('exit', 0)
  assert.deepEqual(fallbacks, [])
})

test('openWslExternalUrl keeps mailto metacharacters as one argv element', () => {
  const { calls, fallbacks } = launch(MAILTO_META)

  assert.equal(calls.length, 1)
  assertSafeLaunch(calls[0], MAILTO_META)
  assert.equal(calls[0].args[1], MAILTO_META)
  assert.deepEqual(fallbacks, [])
})

test('openWslExternalUrl cannot select the old cmd.exe /c start route', () => {
  const launched = wslWindowsOpenUrlArgs(HTTPS_META)
  const { calls } = launch(HTTPS_META)

  assert.notEqual(launched.command.toLowerCase(), 'cmd.exe')
  assert.equal(
    launched.args.some(arg => arg === '/c' || arg === 'start' || arg === '""' || arg === ''),
    false
  )
  assert.notEqual(calls[0].command.toLowerCase(), 'cmd.exe')
  assert.equal(calls[0].args.includes('start'), false)
  assert.deepEqual(calls[0].args, launched.args)
})

test('spawn error falls back once', () => {
  const { child, fallbacks, logs } = launch(HTTPS_META)

  child.emit('error', new Error('ENOENT'))
  child.emit('error', new Error('ENOENT again'))
  assert.deepEqual(fallbacks, [HTTPS_META])
  assert.equal(logs.some(line => line.includes('ENOENT')), true)
})

test('nonzero exit falls back once', () => {
  const { child, fallbacks } = launch(HTTPS_META)

  child.emit('exit', 1)
  child.emit('close', 1)
  assert.deepEqual(fallbacks, [HTTPS_META])
})

test('error followed by exit still falls back once', () => {
  const { child, fallbacks } = launch(HTTPS_META)

  child.emit('error', new Error('spawn failed'))
  child.emit('exit', 1)
  child.emit('close', 1)
  assert.deepEqual(fallbacks, [HTTPS_META])
})

test('successful exit does not fall back', () => {
  const { child, fallbacks } = launch(HTTPS_META)

  child.emit('exit', 0)
  child.emit('close', 0)
  assert.deepEqual(fallbacks, [])
})
