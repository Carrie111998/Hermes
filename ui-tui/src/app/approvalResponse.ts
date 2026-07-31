import type { ApprovalReq } from '../types.js'

import { getOverlayState, patchOverlayState } from './overlayStore.js'

export function approvalResponseParams(request: Pick<ApprovalReq, 'approvalId'>, choice: string, sessionId: string) {
  return { approval_id: request.approvalId, choice, session_id: sessionId }
}

export function clearApprovalById(approvalId: string): boolean {
  if (getOverlayState().approval?.approvalId !== approvalId) {
    return false
  }

  patchOverlayState({ approval: null })

  return true
}
