import type { MutableRefObject } from 'react'

export interface PendingSessionActivation {
  requestId: number
  storedSessionId: string
}

export type SessionActivationRef = MutableRefObject<PendingSessionActivation | null>

export function beginSessionActivation(
  ref: SessionActivationRef | undefined,
  requestId: number,
  storedSessionId: string
): void {
  if (ref) {
    ref.current = { requestId, storedSessionId }
  }
}

export function acknowledgeSessionActivation(
  ref: SessionActivationRef | undefined,
  requestId: number
): void {
  if (ref?.current?.requestId === requestId) {
    ref.current = null
  }
}

export function isSessionActivationPending(
  ref: SessionActivationRef | undefined,
  storedSessionId: string | null
): boolean {
  return Boolean(storedSessionId && ref?.current?.storedSessionId === storedSessionId)
}
