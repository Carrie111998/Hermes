import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeSessionId, $selectedStoredSessionId, $sessions } from '@/store/session'

import { $previewChat } from './preview-chat'
import { PreviewChatControl } from './preview-chat-control'

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)

beforeEach(() => {
  $previewChat.set(null)
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
    }
  ])
})

afterEach(() => {
  cleanup()
  $previewChat.set(null)
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  $sessions.set([])
})

describe('PreviewChatControl', () => {
  it('pins the selected chat', () => {
    const rendered = render(<PreviewChatControl />)

    fireEvent.click(rendered.getByRole('button', { name: 'Select page for chat' }))
    fireEvent.click(rendered.getByRole('option', { name: /Main chat/ }))

    expect($previewChat.get()).toBe('run-main')
  })

  it('clears the pin', () => {
    $previewChat.set('run-main')
    const rendered = render(<PreviewChatControl />)

    fireEvent.click(rendered.getByRole('button', { name: 'Select page for chat' }))
    fireEvent.click(rendered.getByRole('option', { name: /Active tab/ }))

    expect($previewChat.get()).toBeNull()
  })
})
