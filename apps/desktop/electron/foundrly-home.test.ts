import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { assertFoundrlyIsolatedHome, resolveFoundrlyEffectiveHermesHome } from './foundrly-home'

test('resolves default and named Foundrly profiles under the isolated root', () => {
  const base = path.join(path.sep, 'tmp', 'foundrly', 'hermes-home')

  assert.equal(resolveFoundrlyEffectiveHermesHome(base, null), base)
  assert.equal(resolveFoundrlyEffectiveHermesHome(base, 'default'), base)
  assert.equal(
    resolveFoundrlyEffectiveHermesHome(base, 'ops'),
    path.join(base, 'profiles', 'ops')
  )
})

test('refuses an explicit IX default home but accepts a dedicated Foundrly path', () => {
  const ixDefault = path.join(path.sep, 'home', 'owner', '.hermes')
  const dedicated = path.join(path.sep, 'home', 'owner', '.foundrly-hermes')

  assert.throws(() => assertFoundrlyIsolatedHome(ixDefault, ixDefault), /refuses the shared/)
  assert.equal(assertFoundrlyIsolatedHome(dedicated, ixDefault), path.resolve(dedicated))
})
