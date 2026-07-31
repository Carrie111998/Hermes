import { beforeEach, describe, expect, it } from 'vitest'

import { approvalResponseParams, clearApprovalById } from '../app/approvalResponse.js'
import { getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'

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

  it('does not clear a newer approval after an older response completes', () => {
    patchOverlayState({
      approval: {
        approvalId: 'approval-b',
        command: 'rm /tmp/b',
        description: 'delete another file'
      }
    })

    clearApprovalById('approval-a')

    expect(getOverlayState().approval?.approvalId).toBe('approval-b')
  })
})
