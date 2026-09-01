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
    prepareExistingDesktopSession: vi.fn(async () => 'runtime-existing'),
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
      sessionId: 'runtime-existing',
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

  it('rejects a request for a different Desktop profile before lookup or submission', async () => {
    const d = deps()
    d.activeProfile = 'research'

    const receipt = await submitChatZRequest({ ...base, sessionId: 'stored-2' }, d)

    expect(receipt).toMatchObject({ status: 'error', code: 'profile-mismatch' })
    expect(d.requestGateway).not.toHaveBeenCalled()
    expect(d.submitText).not.toHaveBeenCalled()
  })
})
