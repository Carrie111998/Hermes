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
  const idOf = (value: T): string | undefined => (value as { requestId?: string }).requestId

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

// Approval responses carry the core approval id as well as the session. This
// keeps concurrent waiters in one session distinct.
export interface ApprovalRequest extends KeyedPrompt {
  approvalId?: string
  toolCallId?: string
  // false when the backend won't honor a permanent allow (tirith warning) → hide "Always allow".
  allowPermanent?: boolean
  choices?: string[]
  command: string
  description: string
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

interface ApprovalPromptStore {
  $active: ReadableAtom<ApprovalRequest | null>
  $all: ReadableAtom<Record<string, ApprovalRequest[]>>
  clear: (sessionId?: string | null, approvalId?: string) => void
  reset: () => void
  set: (request: ApprovalRequest) => void
}

function approvalPromptStore(): ApprovalPromptStore {
  const $all = atom<Record<string, ApprovalRequest[]>>({})

  return {
    $active: computed([$all, $activeSessionId], (all, activeId) => all[keyFor(activeId)]?.[0] ?? null),
    $all,
    reset: () => $all.set({}),
    set(request) {
      const key = keyFor(request.sessionId)
      const current = $all.get()[key] ?? []
      let nextQueue: ApprovalRequest[]

      if (!request.approvalId) {
        // Backward-compatible gateways expose no stable identity, so only one
        // legacy prompt can be represented safely.
        nextQueue = [request]
      } else {
        const index = current.findIndex(item => item.approvalId === request.approvalId)
        nextQueue =
          index >= 0
            ? current.map((item, itemIndex) => (itemIndex === index ? request : item))
            : [...current, request]
      }

      $all.set({ ...$all.get(), [key]: nextQueue })
    },
    clear(sessionId, approvalId) {
      const all = $all.get()

      if (sessionId !== undefined) {
        const key = keyFor(sessionId)
        const current = all[key] ?? []
        const remaining = approvalId ? current.filter(item => item.approvalId !== approvalId) : []

        if (remaining.length === current.length) {
          return
        }

        const next = { ...all }

        if (remaining.length) {
          next[key] = remaining
        } else {
          delete next[key]
        }

        $all.set(next)

        return
      }

      if (!approvalId) {
        $all.set({})

        return
      }

      const next = Object.fromEntries(
        Object.entries(all)
          .map(([key, queue]) => [key, queue.filter(item => item.approvalId !== approvalId)] as const)
          .filter(([, queue]) => queue.length)
      )

      $all.set(next)
    }
  }
}

const approval = approvalPromptStore()
const sudo = keyedPromptStore<SudoRequest>()
const secret = keyedPromptStore<SecretRequest>()
const resolvedApprovalIds = new Set<string>()
const MAX_RESOLVED_APPROVAL_IDS = 1000

const rememberResolvedApprovalId = (approvalId?: string) => {
  if (!approvalId) {
    return
  }

  resolvedApprovalIds.delete(approvalId)
  resolvedApprovalIds.add(approvalId)

  while (resolvedApprovalIds.size > MAX_RESOLVED_APPROVAL_IDS) {
    const oldest = resolvedApprovalIds.values().next().value

    if (typeof oldest !== 'string') {
      break
    }

    resolvedApprovalIds.delete(oldest)
  }
}

// Inline approval anchors, keyed by session: a tile's inline bar mounting must
// not suppress the PRIMARY session's floating fallback (and vice versa).
const $approvalInlineAnchors = atom<Record<string, number>>({})

export const $approvalRequest = approval.$active

export const setApprovalRequest = (request: ApprovalRequest) => {
  if (request.approvalId && resolvedApprovalIds.has(request.approvalId)) {
    return
  }

  approval.set(request)
}

export const clearApprovalRequest = (sessionId?: string | null, approvalId?: string) => {
  rememberResolvedApprovalId(approvalId)
  approval.clear(sessionId, approvalId)
}

/** The prompt request for one specific session — the tile counterpart of the
 *  active-session `$*Request` views (same map, fixed key). */
export const sessionApprovalRequest = (sessionId: string | null) =>
  computed(approval.$all, all => all[keyFor(sessionId)]?.[0] ?? null)
export const sessionSudoRequest = (sessionId: string | null) =>
  computed(sudo.$all, all => all[keyFor(sessionId)] ?? null)
export const sessionSecretRequest = (sessionId: string | null) =>
  computed(secret.$all, all => all[keyFor(sessionId)] ?? null)

export function registerApprovalInlineAnchor(sessionId: string | null): () => void {
  const key = keyFor(sessionId)

  const bump = (delta: number) => {
    const all = $approvalInlineAnchors.get()
    const next = Math.max(0, (all[key] ?? 0) + delta)
    $approvalInlineAnchors.set({ ...all, [key]: next })
  }

  bump(1)

  return () => bump(-1)
}

/** True when session `sessionId` has an inline approval bar mounted, so its
 *  floating fallback should stand down. Per-session (not global). */
export const sessionApprovalInlineVisible = (sessionId: string | null) =>
  computed($approvalInlineAnchors, anchors => (anchors[keyFor(sessionId)] ?? 0) > 0)

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

    return Boolean(clarify[key] || approvals[key]?.length || sudos[key] || secrets[key])
  })
}

// Drop in-flight prompts for `sessionId` (a turn ended) across all three kinds —
// or every parked prompt when no session is given (global reset / tests).
export function clearAllPrompts(sessionId?: string | null): void {
  if (sessionId === undefined) {
    for (const queue of Object.values(approval.$all.get())) {
      for (const request of queue) {
        rememberResolvedApprovalId(request.approvalId)
      }
    }

    approval.reset()
    sudo.reset()
    secret.reset()
    $approvalInlineAnchors.set({})

    return
  }

  for (const request of approval.$all.get()[keyFor(sessionId)] ?? []) {
    rememberResolvedApprovalId(request.approvalId)
  }

  approval.clear(sessionId)
  sudo.clear(sessionId)
  secret.clear(sessionId)
}
