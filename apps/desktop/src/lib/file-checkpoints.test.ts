import { describe, expect, it } from 'vitest'

import {
  firstUsePromptStorageKey,
  hasSeenFileCheckpointFirstUse,
  isCheckpointsEnabledInConfig,
  markFileCheckpointFirstUseSeen,
  parseFileCheckpointList,
  restoreFileCheckpointParams,
  withCheckpointsEnabled
} from './file-checkpoints'

describe('file checkpoint helpers', () => {
  it('scopes the first-use prompt per profile', () => {
    expect(firstUsePromptStorageKey('coder')).toBe('hermes.file-checkpoints.first-use:coder')
    expect(firstUsePromptStorageKey('  ')).toBe('hermes.file-checkpoints.first-use:default')
  })

  it('records and reads first-use dismissal without nagging again', () => {
    const store = new Map<string, string>()
    const storage = {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => {
        store.set(key, value)
      }
    }

    expect(hasSeenFileCheckpointFirstUse('default', storage)).toBe(false)
    markFileCheckpointFirstUseSeen('default', storage)
    expect(hasSeenFileCheckpointFirstUse('default', storage)).toBe(true)
    expect(hasSeenFileCheckpointFirstUse('other', storage)).toBe(false)
  })

  it('reads checkpoints.enabled from the config record', () => {
    expect(isCheckpointsEnabledInConfig(null)).toBe(false)
    expect(isCheckpointsEnabledInConfig({ checkpoints: false })).toBe(false)
    expect(isCheckpointsEnabledInConfig({ checkpoints: { enabled: false } })).toBe(false)
    expect(isCheckpointsEnabledInConfig({ checkpoints: { enabled: true, max_snapshots: 20 } })).toBe(true)
    expect(isCheckpointsEnabledInConfig({ checkpoints: true })).toBe(true)
  })

  it('enables checkpoints without dropping existing size caps', () => {
    expect(withCheckpointsEnabled({ checkpoints: { max_snapshots: 11 } })).toEqual({
      checkpoints: { enabled: true, max_snapshots: 11 }
    })
  })

  it('asks the gateway to restore files without rewinding the session', () => {
    expect(restoreFileCheckpointParams('sid-1', 'abc123')).toEqual({
      hash: 'abc123',
      rewind_history: false,
      session_id: 'sid-1'
    })
  })

  it('parses rollback.list payloads', () => {
    expect(parseFileCheckpointList(null)).toEqual({ checkpoints: [], enabled: false })
    expect(
      parseFileCheckpointList({
        checkpoints: [{ hash: 'aaa', message: 'turn 1', timestamp: '2026-08-25T00:00:00Z' }, { hash: '' }],
        enabled: true
      })
    ).toEqual({
      checkpoints: [{ hash: 'aaa', message: 'turn 1', timestamp: '2026-08-25T00:00:00Z' }],
      enabled: true
    })
  })
})
