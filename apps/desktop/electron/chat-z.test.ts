import { describe, expect, it } from 'vitest'

import { ChatZRequestError, parseChatZRequest } from './chat-z'

const ID = '123e4567-e89b-42d3-a456-426614174000'
const NOW = 1_000

function base() {
  return {
    version: 1,
    requestId: ID,
    profile: 'default',
    text: 'Run this',
    createdAt: NOW,
    expiresAt: NOW + 30_000
  }
}

describe('parseChatZRequest', () => {
  it('accepts a fixed title on a project-scoped new session', () => {
    expect(
      parseChatZRequest({ ...base(), newSession: true, cwd: 'C:\\project', newTitle: 'Knowledge receiver' }, ID, NOW)
    ).toMatchObject({ newSession: true, cwd: 'C:\\project', newTitle: 'Knowledge receiver' })
  })

  it('rejects a new title on an existing-session target', () => {
    expect(() =>
      parseChatZRequest({ ...base(), sessionId: 'stored-1', newTitle: 'Wrong target' }, ID, NOW)
    ).toThrowError(ChatZRequestError)
  })

  it('requires exactly one target', () => {
    expect(() => parseChatZRequest({ ...base(), sessionId: 'stored-1', title: 'Receiver' }, ID, NOW)).toThrowError(
      /exactly one target/i
    )
  })

  it('rejects expired requests before routing them to the renderer', () => {
    expect(() => parseChatZRequest({ ...base(), sessionId: 'stored-1' }, ID, NOW + 30_001)).toThrowError(/expired/i)
  })
})
