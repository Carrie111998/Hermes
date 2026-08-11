import assert from 'node:assert/strict'
import { test } from 'vitest'

import { checkWindowsRuntime, isDlopenFailure } from '../scripts/assert-win-vcruntime.mjs'

// The real failure, as Node reports it when VCRUNTIME140.dll is absent: the
// message names the addon and nothing else, which is why the guard exists.
function dlopenError() {
  const err = new Error(
    'The specified module could not be found.\\\\?\\C:\\repo\\node_modules\\' +
      '@electron-internal\\extract-zip\\index.win32-x64-msvc.node'
  )
  err.code = 'ERR_DLOPEN_FAILED'
  return err
}

const neverLoads = () => {
  throw new Error('load() should not be called off Windows')
}

test('no-ops off Windows without even attempting the load', async () => {
  for (const platform of ['linux', 'darwin']) {
    const result = await checkWindowsRuntime({ platform, arch: 'x64', load: neverLoads })
    assert.equal(result.ok, true)
    assert.equal(result.skipped, 'not Windows')
  }
})

test('passes on Windows when the addon loads', async () => {
  const result = await checkWindowsRuntime({
    platform: 'win32',
    arch: 'x64',
    load: async () => ({}),
  })
  assert.deepEqual(result, { ok: true })
})

test('fails on Windows when the addon cannot be dlopened', async () => {
  const result = await checkWindowsRuntime({
    platform: 'win32',
    arch: 'x64',
    load: async () => {
      throw dlopenError()
    },
  })
  assert.equal(result.ok, false)
  assert.match(result.error, /Visual C\+\+/)
  assert.match(result.error, /vc_redist\.x64\.exe/)
})

test('points arm64 hosts at the arm64 redistributable', async () => {
  const result = await checkWindowsRuntime({
    platform: 'win32',
    arch: 'arm64',
    load: async () => {
      throw dlopenError()
    },
  })
  assert.equal(result.ok, false)
  assert.match(result.error, /vc_redist\.arm64\.exe/)
})

test('stays out of the way when the addon is merely absent', async () => {
  // A different Electron version, or a partial install: not this guard's
  // problem, and failing the build over it would be worse than silence.
  const err = new Error("Cannot find package '@electron-internal/extract-zip'")
  err.code = 'ERR_MODULE_NOT_FOUND'
  const result = await checkWindowsRuntime({
    platform: 'win32',
    arch: 'x64',
    load: async () => {
      throw err
    },
  })
  assert.equal(result.ok, true)
  assert.match(result.skipped, /ERR_MODULE_NOT_FOUND/)
})

test('isDlopenFailure recognises the code and the message, and nothing else', () => {
  assert.equal(isDlopenFailure(dlopenError()), true)
  assert.equal(isDlopenFailure(new Error('ERR_DLOPEN_FAILED while loading addon')), true)
  assert.equal(isDlopenFailure(new Error('ENOENT: no such file or directory')), false)
  assert.equal(isDlopenFailure(null), false)
  assert.equal(isDlopenFailure(undefined), false)
})
