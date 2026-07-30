import { strict as assert } from 'node:assert'

import { test } from 'vitest'

import {
  cacheAsyncResult,
  type CodesignRunner,
  resolveMacCodeSigningIdentity
} from './desktop-app-identity'

test('code signing verification is parallel, asynchronous, and cached', async () => {
  const releases: Array<() => void> = []
  const calls: string[][] = []

  const runner: CodesignRunner = args =>
    new Promise(resolve => {
      calls.push(args)
      releases.push(() =>
        resolve({
          error: null,
          stderr:
            args[0] === '-dv'
              ? 'Identifier=com.nousresearch.hermes\nTeamIdentifier=TEAM1'
              : '',
          stdout: ''
        })
      )
    })

  const load = cacheAsyncResult(() =>
    resolveMacCodeSigningIdentity({
      bundlePath: '/Applications/Hermes.app',
      isMac: true,
      isPackaged: true,
      runCodesign: runner
    })
  )

  const first = load()
  const second = load()

  assert.equal(first, second)
  assert.equal(calls.length, 2)
  releases.forEach(release => release())
  assert.equal(await first, 'TEAM1:com.nousresearch.hermes')
  assert.equal(calls.length, 2)
})

test('a failed verification is cached for the app run', async () => {
  let calls = 0

  const load = cacheAsyncResult(() => {
    calls += 1

    return Promise.reject(new Error('unavailable'))
  })

  await assert.rejects(load(), /unavailable/)
  await assert.rejects(load(), /unavailable/)
  assert.equal(calls, 1)
})
