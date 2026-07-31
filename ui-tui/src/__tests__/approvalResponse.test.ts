import { beforeEach, describe, expect, it } from 'vitest'

import {
  approvalResponseParams,
  approvalStatusAfterResponse,
  clearApprovalById
} from '../app/approvalResponse.js'
import { enqueueApproval, getOverlayState, resetFlowOverlays, resetOverlayState } from '../app/overlayStore.js'

describe('approval responses', () => {
  beforeEach(() => resetOverlayState())

  it('returns the exact approval id with the decision', () => {
    expect(
      approvalResponseParams(
        {
          approvalId: 'approval-a',
          command: 'rm /tmp/a',
          description: 'delete a file'
        },
        'once',
        'session-1'
      )
    ).toEqual({ approval_id: 'approval-a', choice: 'once', session_id: 'session-1' })
  })

  it('reveals the next queued approval after resolving the first', () => {
    enqueueApproval({ approvalId: 'approval-a', command: 'rm /tmp/a', description: 'delete a file' })
    enqueueApproval({ approvalId: 'approval-b', command: 'rm /tmp/b', description: 'delete another file' })

    expect(getOverlayState().approvals[0]?.approvalId).toBe('approval-a')

    clearApprovalById('approval-a')

    expect(getOverlayState().approvals[0]?.approvalId).toBe('approval-b')
    expect(approvalStatusAfterResponse()).toBe('approval needed')
  })

  it('does not clear the next approval after an older response completes late', () => {
    enqueueApproval({ approvalId: 'approval-a', command: 'rm /tmp/a', description: 'delete a file' })
    enqueueApproval({ approvalId: 'approval-b', command: 'rm /tmp/b', description: 'delete another file' })

    clearApprovalById('approval-a')

    clearApprovalById('approval-a')

    expect(getOverlayState().approvals[0]?.approvalId).toBe('approval-b')
  })

  it('clears the approval queue at turn teardown', () => {
    enqueueApproval({ approvalId: 'approval-a', command: 'rm /tmp/a', description: 'delete a file' })
    enqueueApproval({ approvalId: 'approval-b', command: 'rm /tmp/b', description: 'delete another file' })

    resetFlowOverlays()

    expect(getOverlayState().approvals).toEqual([])
    expect(approvalStatusAfterResponse()).toBe('running…')
  })
})
