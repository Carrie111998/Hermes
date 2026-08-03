import { atom } from 'nanostores'

export interface ContinueOnPhoneTarget {
  profile: string
  sessionId: string
}

export const $continueOnPhoneTarget = atom<ContinueOnPhoneTarget | null>(null)

export function openContinueOnPhone(sessionId: string, profile?: string): boolean {
  const targetSessionId = sessionId.trim()

  if (!targetSessionId) {
    return false
  }

  $continueOnPhoneTarget.set({ profile: profile?.trim() || '', sessionId: targetSessionId })

  return true
}

export function closeContinueOnPhone(): void {
  $continueOnPhoneTarget.set(null)
}
