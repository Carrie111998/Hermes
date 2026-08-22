import assert from 'node:assert/strict'

import { test } from 'vitest'

import { customBuildUpdateBlock } from './update-policy'

test('protects packaged local feature builds from official in-app updates', () => {
  assert.match(
    customBuildUpdateBlock({ branch: 'feat/account-resource-control', source: 'local' }) ?? '',
    /custom build/i
  )
})

test('allows official main builds and unknown stamps to update normally', () => {
  assert.equal(customBuildUpdateBlock({ branch: 'main', source: 'local' }), null)
  assert.equal(customBuildUpdateBlock({ branch: 'main', source: 'release' }), null)
  assert.equal(customBuildUpdateBlock(null), null)
})
