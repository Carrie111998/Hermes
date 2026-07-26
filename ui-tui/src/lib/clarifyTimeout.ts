export interface ClarifyTimeoutNotice {
  text: string
  urgent: boolean
}

const WARNING_SECONDS = 60

/** Submission-time deadline guard. The countdown refreshes once per second,
 * so answer routing must consult the clock directly rather than stale display
 * state. */
export const clarifyDeadlineExpired = (expiresAtMs?: number, nowMs = Date.now()): boolean =>
  expiresAtMs !== undefined && nowMs >= expiresAtMs

const formatRemaining = (seconds: number): string => {
  const whole = Math.max(0, Math.ceil(seconds))
  const hours = Math.floor(whole / 3600)
  const minutes = Math.floor((whole % 3600) / 60)
  const secs = whole % 60

  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
    : `${minutes}:${String(secs).padStart(2, '0')}`
}

/** User-visible response deadline. The final minute escalates from an
 * informational countdown to an explicit draft-loss warning. */
export const clarifyTimeoutNotice = (remainingSeconds?: number): ClarifyTimeoutNotice | null => {
  if (remainingSeconds === undefined) {
    return null
  }

  const urgent = remainingSeconds <= WARNING_SECONDS
  const remaining = formatRemaining(remainingSeconds)

  return {
    text: urgent
      ? `⚠ response times out in ${remaining} — submit now; your draft will be preserved`
      : `response times out in ${remaining}`,
    urgent
  }
}

/** Restore an expired clarify response without overwriting text the user had
 * already parked in the main composer. */
export const mergePreservedClarifyDraft = (composerDraft: string, clarifyDraft: string): string => {
  if (!composerDraft) {
    return clarifyDraft
  }

  if (!clarifyDraft) {
    return composerDraft
  }

  return `${composerDraft}\n\n${clarifyDraft}`
}
