'use strict'

/**
 * Tests for apps/desktop/electron/backend-exit-diagnosis.ts
 *
 * Run with: npx vitest run electron/backend-exit-diagnosis.test.ts
 * (from apps/desktop; wired into npm test:desktop:platforms)
 */

import assert from 'node:assert/strict'

import { describe, it } from 'vitest'

import { describeBackendExitHint } from './backend-exit-diagnosis'

describe('describeBackendExitHint', () => {
  it('recognizes a blocked native DLL import and points at the Windows guide', () => {
    const logTail =
      'ImportError: DLL load failed while importing _sqlite3: ' +
      '应用程序控制策略已阻止此文件。'

    const hint = describeBackendExitHint(logTail)

    assert.ok(hint)
    assert.match(hint, /Smart App Control/)
    assert.match(hint, /windows-native/)
  })

  it('matches regardless of case', () => {
    const hint = describeBackendExitHint('dll load failed while importing _ssl')
    assert.ok(hint)
  })

  it('returns null for an unrelated exit log', () => {
    assert.equal(describeBackendExitHint('Traceback (most recent call last):\nOSError: address already in use'), null)
  })

  it('returns null for an empty log tail', () => {
    assert.equal(describeBackendExitHint(''), null)
  })
})
