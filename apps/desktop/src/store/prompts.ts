import { atom, computed, type ReadableAtom } from 'nanostores'

import { $clarifyRequest, $clarifyRequests } from './clarify'
import { $activeSessionId } from './session'

// Blocking interactive prompts the gateway raises mid-turn. Each maps to a
// `*.request` event the Python side emits while it blocks the agent thread
// waiting for a `*.respond` RPC. Without a renderer for these, the agent
// silently stalls until its timeout (default 5 min) and the tool is BLOCKED.
//
// Like clarify, every prompt is parked under the runtime session id that raised
// it (not one shared slot), so a *background* session running concurrently can
// raise an approval/sudo/secret prompt and have it wait — surfaced via the
// sidebar "needs input" badge — until the user switches to that chat. The
// exported $*Request view is scoped to the active session, so a background
// prompt never hijacks the foreground.

const keyFor = (sessionId: string | null | undefined): string => sessionId ?? ''

interface KeyedPrompt {
  sessionId: string | null
}

interface PromptStore<T extends KeyedPrompt> {
  $active: ReadableAtom<null | T>
  $all: ReadableAtom<Record<string, T>>
  clear: (sessionId?: string | null, requestId?: string) => void
  reset: () => void
  set: (request: T) => void
}

// One per-session prompt kind: a map keyed by session, plus an active-session
// view for the overlays. `clear` drops one session's entry (a request-id
// mismatch is a no-op so a stale resolve can't wipe a newer prompt); with no
// session hint it drops every entry, optionally filtered by request id.
function keyedPromptStore<T extends KeyedPrompt>(): PromptStore<T> {
  const $all = atom<Record<string, T>>({})

  const idOf = (value: T): string | undefined => {
    const keyed = value as { approvalId?: string; requestId?: string }

    return keyed.requestId ?? keyed.approvalId
  }

  return {
    $active: computed([$all, $activeSessionId], (all, activeId) => all[keyFor(activeId)] ?? null),
    $all,
    reset: () => $all.set({}),
    set: request => $all.set({ ...$all.get(), [keyFor(request.sessionId)]: request }),
    clear(sessionId, requestId) {
      const all = $all.get()

      if (sessionId !== undefined) {
        const key = keyFor(sessionId)
        const current = all[key]

        if (current && !(requestId && idOf(current) !== requestId)) {
          const next = { ...all }
          delete next[key]
          $all.set(next)
        }

        return
      }

      const next = Object.fromEntries(Object.entries(all).filter(([, v]) => requestId && idOf(v) !== requestId))

      if (Object.keys(next).length !== Object.keys(all).length) {
        $all.set(next as Record<string, T>)
      }
    }
  }
}

function queuedPromptStore<T extends KeyedPrompt>(idOf: (request: T) => string) {
  const $all = atom<Record<string, T[]>>({})

  return {
    $active: computed([$all, $activeSessionId], (all, activeId) => all[keyFor(activeId)]?.[0] ?? null),
    $all,
    reset: () => $all.set({}),
    set: (request: T) => {
      const all = $all.get()
      const key = keyFor(request.sessionId)
      const queue = all[key] ?? []
      const existingIndex = queue.findIndex(candidate => idOf(candidate) === idOf(request))
      const nextQueue = [...queue]

      if (existingIndex === -1) {
        nextQueue.push(request)
      } else {
        nextQueue[existingIndex] = request
      }

      $all.set({ ...all, [key]: nextQueue })
    },
    clear(sessionId?: string | null, requestId?: string) {
      const all = $all.get()
      const next = { ...all }
      const keys = sessionId === undefined ? Object.keys(all) : [keyFor(sessionId)]
      let changed = false

      for (const key of keys) {
        const queue = all[key]

        if (!queue) {
          continue
        }

        const remaining = requestId ? queue.filter(request => idOf(request) !== requestId) : []

        if (remaining.length === queue.length) {
          continue
        }

        changed = true

        if (remaining.length) {
          next[key] = remaining
        } else {
          delete next[key]
        }
      }

      if (changed) {
        $all.set(next)
      }
    }
  }
}

// Approval presentation is session-keyed, while approval_id identifies the
// exact backend request so a stale response cannot resolve or clear its successor.
export interface ApprovalRequest extends KeyedPrompt {
  approvalId: string
  // false when the backend won't honor a permanent allow (tirith warning) → hide "Always allow".
  allowPermanent?: boolean
  choices?: string[]
  command: string
  description: string
  profile: string
  smartDenied?: boolean
}

export interface SudoRequest extends KeyedPrompt {
  requestId: string
}

export interface SecretRequest extends KeyedPrompt {
  envVar: string
  prompt: string
  requestId: string
}

const approval = queuedPromptStore<ApprovalRequest>(request => request.approvalId)
const sudo = keyedPromptStore<SudoRequest>()
const secret = keyedPromptStore<SecretRequest>()

export const $approvalRequest = approval.$active
export const setApprovalRequest = approval.set
export const clearApprovalRequest = approval.clear

/** The prompt request for one specific session — the tile counterpart of the
 *  active-session `$*Request` views (same map, fixed key). */
export const sessionApprovalRequest = (sessionId: string | null) =>
  computed(approval.$all, all => all[keyFor(sessionId)]?.[0] ?? null)
export const sessionApprovalCount = (sessionId: string | null) =>
  computed(approval.$all, all => all[keyFor(sessionId)]?.length ?? 0)
export const sessionSudoRequest = (sessionId: string | null) =>
  computed(sudo.$all, all => all[keyFor(sessionId)] ?? null)
export const sessionSecretRequest = (sessionId: string | null) =>
  computed(secret.$all, all => all[keyFor(sessionId)] ?? null)

export const $sudoRequest = sudo.$active
export const setSudoRequest = sudo.set
export const clearSudoRequest = sudo.clear

export const $secretRequest = secret.$active
export const setSecretRequest = secret.set
export const clearSecretRequest = secret.clear

// True when the active session is blocked on the user (clarify question or an
// approval / sudo / secret prompt). Mirrors the pet's `awaitingInput` concept
// (agent/pet/state.py): the turn is paused on you, not working — so callers can
// suppress "thinking" indicators and the Esc-to-interrupt shortcut while you
// decide, instead of treating the wait as an in-flight turn.
export const $activeSessionAwaitingInput = computed(
  [$clarifyRequest, $approvalRequest, $sudoRequest, $secretRequest],
  (clarify, approval, sudo, secret) => Boolean(clarify || approval || sudo || secret)
)

/** Per-session `awaitingInput` — the tile composer's counterpart of
 *  `$activeSessionAwaitingInput` (same sources, fixed session instead of the
 *  active one). */
export function sessionAwaitingInput(sessionId: string | null) {
  return computed([$clarifyRequests, approval.$all, sudo.$all, secret.$all], (clarify, approvals, sudos, secrets) => {
    const key = keyFor(sessionId)

    return Boolean(clarify[key] || approvals[key] || sudos[key] || secrets[key])
  })
}

// Drop in-flight prompts for `sessionId` (a turn ended) across all three kinds —
// or every parked prompt when no session is given (global reset / tests).
export function clearAllPrompts(sessionId?: string | null): void {
  if (sessionId === undefined) {
    approval.reset()
    sudo.reset()
    secret.reset()

    return
  }

  approval.clear(sessionId)
  sudo.clear(sessionId)
  secret.clear(sessionId)
}
