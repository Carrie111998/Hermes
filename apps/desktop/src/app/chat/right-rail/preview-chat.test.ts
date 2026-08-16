import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $activeSessionId, $selectedStoredSessionId, $sessions } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { $previewChat, chatChoices, setPreviewChat } from './preview-chat'

describe('preview chat pin', () => {
  beforeEach(() => {
    $previewChat.set(null)
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $sessions.set([])
    $sessionTiles.set([])
  })

  afterEach(() => {
    $previewChat.set(null)
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $sessions.set([])
    $sessionTiles.set([])
  })

  it('lists the primary chat and open tiles', () => {
    $activeSessionId.set('run-main')
    $selectedStoredSessionId.set('stored-main')
    $sessions.set([
      {
        archived: false,
        cwd: null,
        ended_at: null,
        id: 'stored-main',
        input_tokens: 0,
        is_active: true,
        last_active: 1,
        message_count: 1,
        model: null,
        output_tokens: 0,
        parent_session_id: null,
        preview: null,
        source: 'desktop',
        started_at: 1,
        title: 'Main chat',
        tool_call_count: 0
      },
      {
        archived: false,
        cwd: null,
        ended_at: null,
        id: 'stored-tile',
        input_tokens: 0,
        is_active: true,
        last_active: 1,
        message_count: 1,
        model: null,
        output_tokens: 0,
        parent_session_id: null,
        preview: null,
        source: 'desktop',
        started_at: 1,
        title: 'Side tile',
        tool_call_count: 0
      }
    ])
    $sessionTiles.set([{ storedSessionId: 'stored-tile', runtimeId: 'run-tile' }])

    expect(chatChoices()).toEqual([
      { id: 'run-main', kind: 'primary', label: 'Main chat' },
      { id: 'run-tile', kind: 'tile', label: 'Side tile' }
    ])
  })

  it('stores a pin', () => {
    setPreviewChat('run-tile')
    expect($previewChat.get()).toBe('run-tile')
    setPreviewChat(null)
    expect($previewChat.get()).toBeNull()
  })
})
