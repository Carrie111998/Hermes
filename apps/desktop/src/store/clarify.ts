import { atom, computed } from 'nanostores'

import { $gateway } from './gateway'
import { $activeSessionId } from './session'

export interface ClarifyQuestion {
  /** Server-generated wire id (q0..qN) — clarify.respond keys answers by it. */
  qid: string
  question: string
  choices: string[] | null
  multiSelect: boolean
}

export interface ClarifyRequest {
  requestId: string
  question: string
  choices: string[] | null
  multiSelect: boolean
  sessionId: string | null
  /** Batch (multi-question) clarify: present instead of question/choices. */
  questions?: ClarifyQuestion[]
  /** Answers already locked server-side (reconnect replay): qid → answer. */
  lockedAnswers?: Record<string, string>
  /**
   * The model `tool_call_id` of the clarify tool call that raised THIS request,
   * bound once as a validated alias of `requestId`.
   *
   * Renderer-side correlation only — never a wire field, never sent anywhere.
   * The gateway mints `request_id` in `_block` separately from the model's
   * `tool_call_id`, and `tool.complete` comes back carrying only the model id.
   * When this renderer answers the card itself it clears the store locally and
   * the split never shows; when ANOTHER renderer/client answers, that
   * completion is the only settlement signal this one gets. Binding the id at
   * the point the two are already proven to be one clarify — the same-epoch row
   * join — is what lets that completion settle the request instead of stranding
   * it for a later activation to reproject as a live card that nothing answers.
   */
  toolCallId?: string
}

/**
 * The backend labels the agent's recommended option by appending this to the
 * first choice (`tools/clarify_tool.py::mark_recommended`). The renderer never
 * writes it — it only styles it, and discounts it when measuring a choice so a
 * long option isn't dropped for length the label added.
 */
export const RECOMMENDED_LABEL = '(Recommended)'

export const bareChoice = (choice: string): string =>
  choice.endsWith(RECOMMENDED_LABEL) ? choice.slice(0, -RECOMMENDED_LABEL.length).trim() : choice

/**
 * Validate and normalize a choices array.
 *
 * Keeps non-blank, newline-free strings of length ≤ 200; drops everything else
 * and returns an empty array when nothing usable survives — the caller then
 * falls back to a free-text answer instead of dead buttons.
 */
export function normalizeChoices(choices: unknown): string[] {
  if (!Array.isArray(choices)) {
    return []
  }

  return choices.filter(
    (c): c is string => typeof c === 'string' && c.trim().length > 0 && bareChoice(c).length <= 200 && !c.includes('\n')
  )
}

/**
 * Structured warning for a clarify payload that arrived with choices but had
 * them all normalized away — keeps the remaining #69122 "no selectable choices"
 * triggers diagnosable in the field without dead constant fields.
 */
export function warnDroppedChoices(source: 'gateway' | 'tool_args', question: string, rawChoices: unknown): void {
  console.warn('[clarify] choices dropped after normalization', {
    choices_count: Array.isArray(rawChoices) ? rawChoices.length : 0,
    question_length: question.length,
    source
  })
}

/**
 * Validate and normalize a batch clarify payload's `questions` array.
 *
 * Keeps entries with a non-blank string `qid` and `question`; per-question
 * choices go through `normalizeChoices` (all-blank → open-ended) and
 * multi_select is only honored alongside surviving choices. Returns an empty
 * array when nothing usable remains — the caller treats that as "not a
 * batch" instead of rendering an unanswerable form.
 */
export function normalizeQuestions(questions: unknown): ClarifyQuestion[] {
  if (!Array.isArray(questions)) {
    return []
  }

  const normalized: ClarifyQuestion[] = []

  for (const entry of questions) {
    if (typeof entry !== 'object' || entry === null) {
      continue
    }

    const row = entry as Record<string, unknown>
    const qid = typeof row.qid === 'string' ? row.qid.trim() : ''
    const question = typeof row.question === 'string' ? row.question.trim() : ''

    if (!qid || !question) {
      continue
    }

    const choices = normalizeChoices(row.choices)

    normalized.push({
      choices: choices.length > 0 ? choices : null,
      multiSelect: row.multi_select === true && choices.length > 0,
      qid,
      question
    })
  }

  return normalized
}

// Pending clarify requests keyed by the runtime session id that raised them.
// Storing per-session (instead of one shared slot) lets a *background* session
// park its clarify request while the user is looking at a different chat, then
// resolve it once they switch over — without a second concurrent clarify
// clobbering the first. A request with no session id lands under the empty key.
const keyFor = (sessionId: string | null | undefined): string => sessionId ?? ''

export const $clarifyRequests = atom<Record<string, ClarifyRequest>>({})

// The clarify request for the currently-viewed session. The inline ClarifyTool
// only ever mounts inside the active session's transcript, so it reads this
// focus-scoped view rather than reaching into the whole map.
export const $clarifyRequest = computed(
  [$clarifyRequests, $activeSessionId],
  (requests, activeId) => requests[keyFor(activeId)] ?? null
)

/** The clarify request for one specific session — the tile counterpart of the
 *  active-session `$clarifyRequest` view (same map, fixed key). */
export const sessionClarifyRequest = (sessionId: string | null) =>
  computed($clarifyRequests, requests => requests[keyFor(sessionId)] ?? null)

export function setClarifyRequest(request: ClarifyRequest): void {
  $clarifyRequests.set({ ...$clarifyRequests.get(), [keyFor(request.sessionId)]: request })
}

/**
 * The one correlation key a clarify's two identities share: its question text,
 * or the batch list joined in order. Built the same way on both sides — raw
 * `tool.start` args and the normalized request — so the join is exact. The
 * separator is NUL because it cannot occur inside a question, so no regrouping
 * of a batch can ever produce another batch's key.
 */
function questionKeyOf(question: unknown, questions: unknown): string {
  if (Array.isArray(questions) && questions.length > 0) {
    return questions
      .map(entry =>
        entry !== null && typeof entry === 'object' && typeof (entry as { question?: unknown }).question === 'string'
          ? (entry as { question: string }).question.trim()
          : ''
      )
      .join('\0')
  }

  return typeof question === 'string' ? question.trim() : ''
}

/**
 * The clarify tool call this session just started, held until the gateway's
 * `clarify.request` for it lands — a one-slot handoff, never a history.
 *
 * `tool.start` is the only frame that names the model's `tool_call_id` before
 * the tool blocks, so it is the only place the alias can be picked up. The slot
 * is replaced by each new clarify start, consumed by the request it binds to,
 * and dropped when the tool completes, so nothing from a settled or older epoch
 * survives to be joined to a later request.
 */
const startedClarifyToolCalls = new Map<string, { questionKey: string; toolCallId: string }>()

export function noteClarifyToolCall(
  sessionId: string | null | undefined,
  started: { args?: unknown; toolCallId?: unknown } | null
): void {
  const key = keyFor(sessionId)

  if (!started) {
    startedClarifyToolCalls.delete(key)

    return
  }

  const toolCallId = typeof started.toolCallId === 'string' ? started.toolCallId : ''
  const args = (started.args ?? {}) as Record<string, unknown>
  const questionKey = questionKeyOf(args.question, args.questions)

  // A start with no id or no question carries no evidence of identity, so it
  // must not stand in for one — and it must not leave an older slot behind.
  if (!toolCallId || !questionKey) {
    startedClarifyToolCalls.delete(key)

    return
  }

  startedClarifyToolCalls.set(key, { questionKey, toolCallId })
}

/**
 * The model tool-call id to bind onto the request `requestId` is about to
 * install, or `undefined` when nothing validates the join.
 *
 * Two sources, in order: the alias already bound to this exact request id (a
 * reconnect replays `clarify.request` with no fresh `tool.start` behind it), and
 * the started tool call whose question is this request's question. The second is
 * consumed, so one start can alias exactly one request.
 */
export function clarifyToolCallAlias(
  sessionId: string | null | undefined,
  requestId: string,
  request: { question?: unknown; questions?: unknown }
): string | undefined {
  const key = keyFor(sessionId)
  const current = $clarifyRequests.get()[key]

  if (current?.requestId === requestId && current.toolCallId) {
    return current.toolCallId
  }

  const started = startedClarifyToolCalls.get(key)
  const questionKey = questionKeyOf(request.question, request.questions)

  if (!started || !questionKey || started.questionKey !== questionKey) {
    return undefined
  }

  startedClarifyToolCalls.delete(key)

  return started.toolCallId
}

/**
 * Bind a model tool-call id that row/request reconciliation has just proven
 * belongs to `requestId`, when the record does not already carry one.
 *
 * The cold-resume path rebuilds the canonical record from `pending_clarify`,
 * which knows only the gateway id, while the persisted transcript still holds
 * the row the model started. Reconciliation joins those two inside one epoch;
 * this records that join so the completion can use it.
 */
export function bindClarifyToolCallAlias(
  sessionId: string | null | undefined,
  requestId: string,
  toolCallId: string
): void {
  const key = keyFor(sessionId)
  const requests = $clarifyRequests.get()
  const current = requests[key]

  if (!current || current.requestId !== requestId || current.toolCallId || !toolCallId) {
    return
  }

  $clarifyRequests.set({ ...requests, [key]: { ...current, toolCallId } })
}

export function clearClarifyRequest(requestId?: string, sessionId?: string | null): void {
  const requests = $clarifyRequests.get()

  // Targeted clear when the caller knows the session (the common path from the
  // inline ClarifyTool answering its own request).
  if (sessionId !== undefined) {
    const key = keyFor(sessionId)
    const current = requests[key]

    if (!current || (requestId && current.requestId !== requestId)) {
      return
    }

    const next = { ...requests }
    delete next[key]
    $clarifyRequests.set(next)

    return
  }

  // Fallback with no session hint: drop every entry matching the request id
  // (or clear all when none is given).
  const next: Record<string, ClarifyRequest> = {}
  let changed = false

  for (const [key, value] of Object.entries(requests)) {
    if (requestId && value.requestId !== requestId) {
      next[key] = value
    } else {
      changed = true
    }
  }

  if (changed) {
    $clarifyRequests.set(next)
  }
}

/** Whether `sessionId` has a clarify parked on it right now (imperative read —
 *  the composer checks this on Enter, not on every render). */
export const hasClarifyRequest = (sessionId: string | null | undefined): boolean =>
  Boolean($clarifyRequests.get()[keyFor(sessionId)])

/**
 * The one canonical unresolved request for a runtime identity, read
 * imperatively. Reconciliation runs after hydration settles, outside React, so
 * it reads the authority here rather than through the computed views.
 */
export const unresolvedClarifyRequest = (sessionId: string | null | undefined): ClarifyRequest | null =>
  $clarifyRequests.get()[keyFor(sessionId)] ?? null

/**
 * Move an unresolved request from the identity that raised it onto the runtime
 * identity now rendering the conversation.
 *
 * A cold resume mints a NEW runtime id for the same stored conversation, and
 * the request is keyed by the runtime that raised it — so without this the
 * authority is stranded under a key nothing looks up and the transcript shows
 * "needs input" with no answerable card. Rebinding is a MOVE, not a copy: the
 * old-runtime projection must stop being actionable before the new one is
 * published, or the same request could be answered twice. A newer request
 * already parked on the target identity always wins.
 */
export function rebindClarifyRequest(fromSessionId: string | null | undefined, toSessionId: string | null): boolean {
  const fromKey = keyFor(fromSessionId)
  const toKey = keyFor(toSessionId)

  if (fromKey === toKey) {
    return Boolean($clarifyRequests.get()[toKey])
  }

  const requests = $clarifyRequests.get()
  const carried = requests[fromKey]

  if (!carried) {
    return Boolean(requests[toKey])
  }

  const next = { ...requests }
  delete next[fromKey]

  // Never demote a request the target identity already owns — that one is the
  // newer live epoch.
  if (!next[toKey]) {
    next[toKey] = { ...carried, sessionId: toSessionId }
  }

  $clarifyRequests.set(next)

  return true
}

/**
 * Restore a request that was cleared optimistically for a settlement that then
 * failed. The backend is still blocked on `clarify.respond`, so the card has to
 * become answerable again — but a NEWER request for that identity is a later
 * epoch and must never be overwritten by the restoration of an older one.
 */
export function restoreClarifyRequest(request: ClarifyRequest): boolean {
  const key = keyFor(request.sessionId)
  const requests = $clarifyRequests.get()
  const current = requests[key]

  if (current && current.requestId !== request.requestId) {
    return false
  }

  if (current) {
    return true
  }

  $clarifyRequests.set({ ...requests, [key]: request })

  return true
}

/**
 * Settle exactly the request a completion correlates with.
 *
 * Correlation is request-id first. A model tool-call id may only join through
 * the already-validated active request: the gateway mints `request_id`
 * separately from the model's `tool_call_id`, so the completion of the clarify
 * tool can carry either. Question text is a bounded compatibility aid for the
 * SAME unresolved epoch and never merges two distinct request ids.
 *
 * Returns whether this completion settled the pending request — the caller uses
 * that to decide whether clarify attention may clear. An unrelated tool
 * completing settles nothing.
 */
export function settleClarifyRequest(
  sessionId: string | null | undefined,
  correlation: { question?: string; requestId?: string; toolName?: string }
): boolean {
  const current = $clarifyRequests.get()[keyFor(sessionId)]

  if (!current) {
    return false
  }

  if (correlation.requestId && correlation.requestId === current.requestId) {
    clearClarifyRequest(current.requestId, current.sessionId)

    return true
  }

  // Only the clarify tool itself may settle a clarify by anything other than
  // the request id.
  if (correlation.toolName !== 'clarify') {
    return false
  }

  // The one id besides the gateway's that names THIS request: the model
  // tool-call id bound to it while it was raised. `tool.complete` carries only
  // that id, so a clarify answered by another renderer — which never runs this
  // one's local clear — settles here through it and nowhere else. It is an
  // exact bound value, not a second correlation rule: an id that was never
  // bound to this record still falls through to the epoch guard below.
  if (correlation.requestId && current.toolCallId && correlation.requestId === current.toolCallId) {
    clearClarifyRequest(current.requestId, current.sessionId)

    return true
  }

  // Both sides named a request and they disagree: that is a DIFFERENT epoch,
  // and identical wording is not evidence otherwise. An older clarify that
  // returns late (server timeout, another client answered) must not clear the
  // newer request the backend is still blocked on just because the agent asked
  // the same thing twice. Text is only a compatibility aid for a completion
  // that carries no identity at all — never a way to merge two request ids.
  if (correlation.requestId) {
    return false
  }

  const question = correlation.question?.trim()

  if (question && question === current.question.trim()) {
    clearClarifyRequest(current.requestId, current.sessionId)

    return true
  }

  // A clarify completion with no usable correlator is still this session's only
  // unresolved clarify — the blocked tool just returned.
  if (!correlation.requestId && !question) {
    clearClarifyRequest(current.requestId, current.sessionId)

    return true
  }

  return false
}

/**
 * Answer `sessionId`'s pending clarify with an empty answer (a skip) and drop it
 * locally, resolving to whether there was one to skip.
 *
 * The composer uses this when the user types a real message instead of picking
 * an option: a clarify blocks the agent inside its tool batch, so leaving it
 * unanswered would park the follow-up until the server-side clarify timeout
 * (default 5 min) — the message looks sent and nothing happens. Skipping lets
 * the tool return and the turn carry on with the user's actual words.
 *
 * An empty answer is the same thing the card's own Skip button sends, and
 * `clarify.respond` is `allow_expired`, so racing the timeout is harmless.
 */
export async function skipClarifyRequest(sessionId: string | null | undefined): Promise<boolean> {
  const request = $clarifyRequests.get()[keyFor(sessionId)]

  if (!request) {
    return false
  }

  // Clear first: the answer is already decided, and an in-flight RPC must not
  // leave a live card the user can answer a second time.
  clearClarifyRequest(request.requestId, request.sessionId)

  try {
    await $gateway.get()?.request('clarify.respond', { request_id: request.requestId, answer: '' })
  } catch {
    // The skip never reached the backend, so it is still blocked on
    // `clarify.respond`. Dropping our copy would strand that turn with no
    // answerable card and no way back, so restore the request — the user's
    // message still sends either way.
    restoreClarifyRequest(request)
  }

  return true
}
