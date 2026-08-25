import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import type * as NativeNotificationsStore from '@/store/native-notifications'
import { clearAllPrompts } from '@/store/prompts'
import { setSessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const { dispatchNativeNotification, playApprovalSound } = vi.hoisted(() => ({
  dispatchNativeNotification: vi.fn(() => true),
  playApprovalSound: vi.fn(async () => undefined)
}))

vi.mock('@/lib/approval-sound', () => ({ playApprovalSound }))
vi.mock('@/store/native-notifications', async importOriginal => {
  const actual = await importOriginal<typeof NativeNotificationsStore>()

  return { ...actual, dispatchNativeNotification }
})

const RUNTIME_SID = 'runtime-approval'
const STORED_SID = 'stored-approval'

let stream: MessageStreamHarness

const session = {
  id: STORED_SID,
  profile: 'regular',
  title: 'Deploy notification fix'
} as unknown as SessionInfo

describe('approval attention event', () => {
  beforeEach(() => {
    dispatchNativeNotification.mockReset()
    dispatchNativeNotification.mockReturnValue(true)
    playApprovalSound.mockClear()
    clearAllPrompts()
    setSessions([session])
    stream = renderMessageStream(RUNTIME_SID, {
      states: new Map([[RUNTIME_SID, createClientSessionState(STORED_SID)]])
    })
  })

  afterEach(() => {
    cleanup()
    clearAllPrompts()
    setSessions([])
    vi.restoreAllMocks()
  })

  it('sounds the alarm and names the blocked session in the native notification', () => {
    act(() =>
      stream.handleEvent({
        payload: {
          command: 'safe approval probe',
          description: 'approval probe',
          request_id: 'req-approval'
        },
        profile: 'regular',
        session_id: RUNTIME_SID,
        type: 'approval.request'
      })
    )

    expect(playApprovalSound).toHaveBeenCalledWith(`${RUNTIME_SID}:req-approval`)
    expect(dispatchNativeNotification).toHaveBeenCalledWith(
      expect.objectContaining({
        body: 'safe approval probe',
        kind: 'approval',
        sessionId: RUNTIME_SID,
        title: 'Approval needed · Deploy notification fix'
      })
    )
    expect(stream.state().needsInput).toBe(true)
  })

  it('uses the session title from the exact connection and profile source', () => {
    setSessions([
      { ...session, connection_id: 'source-a', title: 'Wrong source title' },
      { ...session, connection_id: 'source-b', title: 'Correct source title' }
    ])

    act(() =>
      stream.handleEvent({
        connectionId: 'source-b',
        payload: { command: 'safe approval probe', request_id: 'req-scoped' },
        profile: 'regular',
        session_id: RUNTIME_SID,
        type: 'approval.request'
      })
    )

    expect(dispatchNativeNotification).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Approval needed · Correct source title' })
    )
  })

  it('does not sound when notification preferences or replay guards suppress the alert', () => {
    dispatchNativeNotification.mockReturnValue(false)

    act(() =>
      stream.handleEvent({
        payload: { command: 'safe approval probe', request_id: 'req-suppressed' },
        profile: 'regular',
        session_id: RUNTIME_SID,
        type: 'approval.request'
      })
    )

    expect(playApprovalSound).not.toHaveBeenCalled()
    expect(stream.state().needsInput).toBe(true)
  })
})
