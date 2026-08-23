import { describe, expect, it, vi } from 'vitest'

import { type ChatZSubmitRequest, submitChatZRequest } from './chat-z'

const base: Omit<ChatZSubmitRequest, 'cwd' | 'newSession' | 'newTitle' | 'sessionId' | 'title'> = {
  version: 1,
  requestId: 'request-1',
  profile: 'default',
  text: 'Do the work',
  createdAt: Date.now(),
  expiresAt: Date.now() + 30_000
}

function deps() {
  return {
    activeProfile: 'default',
    createDesktopSession: vi.fn(async () => ({ runtimeSessionId: 'runtime-1', storedSessionId: 'stored-1' })),
    getSelectedStoredSessionId: vi.fn(() => null),
    requestGateway: vi.fn(async () => ({})),
    submitText: vi.fn(async () => true)
  }
}

describe('submitChatZRequest', () => {
  it('creates, titles, then submits a project-scoped Desktop session', async () => {
    const d = deps()
    const receipt = await submitChatZRequest(
      { ...base, newSession: true, cwd: 'C:\\project', newTitle: 'Knowledge receiver' },
      d
    )

    expect(d.createDesktopSession).toHaveBeenCalledWith('C:\\project')
    expect(d.requestGateway).toHaveBeenCalledWith('session.title', {
      session_id: 'runtime-1',
      title: 'Knowledge receiver'
    })
    expect(d.submitText).toHaveBeenCalledWith('Do the work', {
      attachments: [],
      fromQueue: true,
      sessionId: 'runtime-1',
      storedSessionId: 'stored-1'
    })
    expect(receipt).toMatchObject({
      status: 'accepted',
      storedSessionId: 'stored-1',
      title: 'Knowledge receiver',
      created: true
    })
  })

  it('resolves an exact title and submits without creating a session', async () => {
    const d = deps()
    d.requestGateway.mockResolvedValue({ sessions: [{ id: 'stored-2', title: 'Receiver' }] })

    const receipt = await submitChatZRequest({ ...base, title: 'Receiver' }, d)

    expect(d.createDesktopSession).not.toHaveBeenCalled()
    expect(d.submitText).toHaveBeenCalledWith('Do the work', {
      attachments: [],
      fromQueue: true,
      storedSessionId: 'stored-2'
    })
    expect(receipt).toMatchObject({ status: 'accepted', storedSessionId: 'stored-2', title: 'Receiver' })
  })

  it('fails clearly when an exact title does not exist', async () => {
    const d = deps()
    d.requestGateway.mockResolvedValue({ sessions: [] })

    const receipt = await submitChatZRequest({ ...base, title: 'Missing' }, d)

    expect(receipt).toMatchObject({ status: 'error', code: 'session-not-found' })
    expect(d.submitText).not.toHaveBeenCalled()
  })
})
