import { normalizeChoices, normalizeQuestions, setClarifyRequest } from '@/store/clarify'
import type { SessionResumeResponse } from '@/types/hermes'

/**
 * Restore a pending clarify from a resume/activate snapshot onto `sessionId`.
 *
 * The snapshot mirrors the live clarify.request wire shape: single-question
 * payloads carry `question`/`choices`/`multi_select`; batch (multi-question)
 * ones carry `questions` (+ any answers already locked server-side) and no
 * top-level `question`. Accepting only the single form left batch cards
 * invisible after every session switch-and-return (#92916).
 *
 * Returns whether a request was restored.
 */
export function restorePendingClarifyForTest(
  response: Pick<SessionResumeResponse, 'pending_clarify'>,
  sessionId: string
): boolean {
  const pending = response.pending_clarify

  if (!pending || typeof pending.request_id !== 'string') {
    return false
  }

  const questions = normalizeQuestions(pending.questions)

  if (questions.length > 0) {
    setClarifyRequest({
      choices: null,
      lockedAnswers:
        pending.answers && typeof pending.answers === 'object'
          ? Object.fromEntries(
              Object.entries(pending.answers as Record<string, unknown>).filter(
                (entry): entry is [string, string] => typeof entry[1] === 'string'
              )
            )
          : undefined,
      multiSelect: false,
      question: '',
      questions,
      requestId: pending.request_id,
      sessionId
    })

    return true
  }

  if (typeof pending.question !== 'string') {
    return false
  }

  const choices = normalizeChoices(pending.choices)

  setClarifyRequest({
    choices: choices.length > 0 ? choices : null,
    multiSelect: pending.multi_select === true,
    question: pending.question,
    requestId: pending.request_id,
    sessionId
  })

  return true
}
