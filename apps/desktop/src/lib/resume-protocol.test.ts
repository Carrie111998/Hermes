import assert from 'node:assert/strict'

import { describe, test } from 'vitest'

import { isSnapshotRequired } from './resume-protocol'
import type { ResumeResult } from './resume-protocol'

describe('resume-protocol (§16 §17)', () => {
  test('truncated → snapshot-required', () => {
    const r: ResumeResult = { kind:'snapshot-required', reason:'truncated', sessions:['a'] }
    assert.equal(isSnapshotRequired(r), true)
  })
  test('replay is not snapshot-required', () => {
    const r: ResumeResult = { kind:'replay', events:[], latest:{} }
    assert.equal(isSnapshotRequired(r), false)
  })
})
