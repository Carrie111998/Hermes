/** Fail-closed native update-apply boundary tests. */
import assert from 'node:assert/strict'

import { test } from 'vitest'

import { nativeUpdateApplyDecision } from './update-apply-boundary'

const available = { supported: true, behind: 2 }

test('permits only a successful native check with a positive count', () => {
  assert.deepEqual(nativeUpdateApplyDecision(available), { ok: true })
})

test.each([
  ['missing', null],
  ['unsupported', { supported: false, behind: 2 }],
  ['failed', { supported: true, behind: 2, error: 'check-failed' }],
  ['current', { supported: true, behind: 0 }],
  ['unknown count', { supported: true }],
  ['invalid negative count', { supported: true, behind: -1 }]
])('refuses %s update status before native transport', (_label, status) => {
  const decision = nativeUpdateApplyDecision(status)
  assert.equal(decision.ok, false)
  assert.equal(decision.error, 'update-not-applyable')
})
