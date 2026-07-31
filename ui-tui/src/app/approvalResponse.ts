import type { ApprovalReq } from '../types.js'

import { clearApproval, getOverlayState } from './overlayStore.js'

export function approvalResponseParams(request: Pick<ApprovalReq, 'approvalId'>, choice: string, sessionId: string) {
  return { approval_id: request.approvalId, choice, session_id: sessionId }
}

export function clearApprovalById(approvalId: string): boolean {
  return clearApproval(approvalId)
}

export function approvalStatusAfterResponse(): string {
  return getOverlayState().approvals.length ? 'approval needed' : 'running…'
}
