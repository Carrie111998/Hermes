import { type RefObject, useCallback, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { useSessionSlice } from '@/lib/use-session-slice'
import { clearComposerAttachments, type ComposerAttachment } from '@/store/composer'
import { resetBrowseState } from '@/store/composer-input-history'
import {
  $queuedPromptsBySession,
  AUTO_DRAIN_RETRY_BASE_MS,
  enqueueQueuedPrompt,
  MAX_AUTO_DRAIN_ATTEMPTS,
  migrateQueuedPrompts,
  promoteQueuedPrompt,
  type QueuedPromptEntry,
  readFreshQueuedPrompts,
  removeQueuedPrompt,
  shouldAutoDrain,
  updateQueuedPrompt,
  whenSessionDrainClaimReleased,
  withSessionDrainClaim
} from '@/store/composer-queue'
import { notify } from '@/store/notifications'

import { cloneAttachments, type QueueEditState } from '../composer-utils'
import type { ChatBarProps } from '../types'

/** How long a manual "send now" waits for another window's in-flight drain
 * before giving up and telling the user (the entry stays queued). */
const MANUAL_SEND_WAIT_MS = 15_000

interface UseComposerQueueArgs {
  activeQueueSessionKey: string | null
  attachments: ComposerAttachment[]
  busy: boolean
  clearDraft: () => void
  draftRef: RefObject<string>
  focusInput: () => void
  loadIntoComposer: (text: string, attachments: ComposerAttachment[]) => void
  onCancel: ChatBarProps['onCancel']
  onSubmit: ChatBarProps['onSubmit']
  queueEditRef: RefObject<QueueEditState | null>
  queueSessionKey: ChatBarProps['queueSessionKey']
  sessionId: string | null | undefined
}

/**
 * The composer's queue engine — everything about queued turns: the per-session
 * queue store binding, in-place queued-prompt editing (begin/step/exit), the
 * drain locks (renderer-local + cross-window claim) + send-then-remove
 * sequence, manual send-now, and the
 * edge-independent auto-drain with bounded retries. It consumes the draft API
 * (draftRef/clearDraft/loadIntoComposer/focusInput) and writes the
 * coordinator-owned `queueEditRef` so the draft engine can read the edit state
 * without a back-reference. Behaviour-identical to the inline original.
 */
export function useComposerQueue({
  activeQueueSessionKey,
  attachments,
  busy,
  clearDraft,
  draftRef,
  focusInput,
  loadIntoComposer,
  onCancel,
  onSubmit,
  queueEditRef,
  queueSessionKey,
  sessionId
}: UseComposerQueueArgs) {
  const { t } = useI18n()

  // Per-session slice (edge): re-renders only when THIS session's queue changes,
  // not on cross-session queue churn (the plain atom's map ref changes on every
  // write; the keyed array does not).
  const queuedPrompts = useSessionSlice($queuedPromptsBySession, activeQueueSessionKey)

  const [queueEdit, setQueueEdit] = useState<QueueEditState | null>(null)
  queueEditRef.current = queueEdit

  const setQueueEditSnapshot = useCallback(
    (next: QueueEditState | null) => {
      queueEditRef.current = next
      setQueueEdit(next)
    },
    [queueEditRef]
  )

  const editingQueuedPrompt = queueEdit ? (queuedPrompts.find(entry => entry.id === queueEdit.entryId) ?? null) : null

  const prevQueueKeyRef = useRef(activeQueueSessionKey)
  const drainingQueueRef = useRef(false)
  const drainFailuresRef = useRef(new Map<string, number>())

  // Liveness plumbing for auto-drain: a bounded-backoff retry timer (rejected
  // sends), a single pending claim-release waiter (lost cross-window races),
  // and a ref to the latest autoDrainNext so both re-trigger fresh logic. The
  // mounted flag stops a late-firing timer/waiter from draining on behalf of
  // an unmounted composer.
  const retryTimerRef = useRef<null | ReturnType<typeof setTimeout>>(null)
  const claimWaiterArmedRef = useRef(false)
  const autoDrainRef = useRef<() => void>(() => {})
  const mountedRef = useRef(true)

  const beginQueuedEdit = (entry: QueuedPromptEntry) => {
    if (!activeQueueSessionKey || queueEdit) {
      return
    }

    setQueueEditSnapshot({
      attachments: cloneAttachments(attachments),
      draft: draftRef.current,
      entryId: entry.id,
      sessionKey: activeQueueSessionKey
    })
    loadIntoComposer(entry.text, entry.attachments)
    triggerHaptic('selection')
    focusInput()
  }

  // Walk queued entries while editing (ArrowUp = older, ArrowDown = newer),
  // saving the in-progress edit on each step. Stepping newer past the last
  // entry exits edit mode and restores the pre-edit draft.
  const stepQueuedEdit = (direction: -1 | 1) => {
    if (!queueEdit) {
      return false
    }

    const index = queuedPrompts.findIndex(e => e.id === queueEdit.entryId)
    const target = index + direction

    if (index < 0 || target < 0) {
      return index >= 0 // at the oldest: swallow; missing entry: let it fall through
    }

    const saved = updateQueuedPrompt(queueEdit.sessionKey, queueEdit.entryId, {
      attachments: cloneAttachments(attachments),
      text: draftRef.current
    })

    const next = queuedPrompts[target]

    if (next) {
      setQueueEditSnapshot({ ...queueEdit, entryId: next.id })
      loadIntoComposer(next.text, next.attachments)
    } else {
      setQueueEditSnapshot(null)
      loadIntoComposer(queueEdit.draft, queueEdit.attachments)
    }

    triggerHaptic(saved ? 'success' : 'selection')
    focusInput()

    return true
  }

  const exitQueuedEdit = (action: 'cancel' | 'save'): boolean => {
    if (!queueEdit) {
      return false
    }

    if (action === 'save') {
      const text = draftRef.current
      const next = cloneAttachments(attachments)

      if (!text.trim() && next.length === 0) {
        return false
      }

      const saved = updateQueuedPrompt(queueEdit.sessionKey, queueEdit.entryId, { attachments: next, text })
      triggerHaptic(saved ? 'success' : 'selection')
    } else {
      triggerHaptic('cancel')
    }

    setQueueEditSnapshot(null)
    loadIntoComposer(queueEdit.draft, queueEdit.attachments)
    focusInput()

    return true
  }

  const queueCurrentDraft = useCallback(() => {
    const text = draftRef.current

    if (!activeQueueSessionKey || (!text.trim() && attachments.length === 0)) {
      return false
    }

    if (!enqueueQueuedPrompt(activeQueueSessionKey, { text, attachments })) {
      return false
    }

    clearDraft()
    clearComposerAttachments()
    triggerHaptic('selection')

    return true
  }, [activeQueueSessionKey, attachments, clearDraft, draftRef])

  // All queue drain paths share one send-then-remove sequence behind two
  // exclusion layers: the renderer-local ref serializes attempts within THIS
  // window, and the session's cross-window drain claim serializes across
  // windows — every idle window schedules auto-drain, so without the claim two
  // of them could pick the same head and each submit it before either reaches
  // the removal below (#46732). `pickEntry` lets each caller choose head,
  // by-id, or skip-edited; the outcome (not a bare boolean) lets auto-drain
  // count only genuine send failures toward its retry cap.
  const runDrain = useCallback(
    async (
      pickEntry: (entries: QueuedPromptEntry[]) => QueuedPromptEntry | undefined,
      claim: { wait?: boolean; timeoutMs?: number } = {}
    ): Promise<'contended' | 'empty' | 'rejected' | 'sent'> => {
      if (drainingQueueRef.current || !activeQueueSessionKey) {
        return 'contended'
      }

      drainingQueueRef.current = true

      try {
        const outcome = await withSessionDrainClaim(
          activeQueueSessionKey,
          async (): Promise<'empty' | 'rejected' | 'sent'> => {
            // Pick INSIDE the claim, and from the fresh store read rather than
            // the rendered slice or the atom: the storage event that syncs them
            // is asynchronous, so only the fresh read reflects a removal
            // another window just made. Picking any earlier (or from anything
            // staler) is what allowed the double submit.
            const entry = pickEntry(readFreshQueuedPrompts(activeQueueSessionKey))

            if (!entry) {
              return 'empty'
            }

            const accepted = await Promise.resolve(
              onSubmit(entry.text, { attachments: entry.attachments, fromQueue: true })
            )

            if (accepted === false) {
              return 'rejected'
            }

            drainFailuresRef.current.delete(entry.id)
            removeQueuedPrompt(activeQueueSessionKey, entry.id)
            resetBrowseState(sessionId)

            return 'sent'
          },
          claim
        )

        // null = the claim was unavailable (or the wait timed out); whoever
        // holds it owns the entry for now.
        return outcome ?? 'contended'
      } finally {
        drainingQueueRef.current = false
      }
    },
    [activeQueueSessionKey, onSubmit, sessionId]
  )

  const pickDrainHead = useCallback(
    (entries: QueuedPromptEntry[]) => {
      const skip = queueEditRef.current?.entryId

      return skip ? entries.find(e => e.id !== skip) : entries[0]
    },
    [queueEditRef] // reads the edit id off a ref so the lock-holder always sees the latest
  )

  const drainNextQueued = useCallback(
    () => runDrain(pickDrainHead).then(outcome => outcome === 'sent'),
    [pickDrainHead, runDrain]
  )

  const sendQueuedNow = useCallback(
    async (id: string) => {
      if (!activeQueueSessionKey || id === queueEdit?.entryId) {
        return false
      }

      // The tapped entry may be a phantom: another window drained or deleted
      // it and our storage-event sync hasn't landed, so the panel still shows
      // it. Bail before interrupting a live turn for something that is gone.
      if (!readFreshQueuedPrompts(activeQueueSessionKey).some(e => e.id === id)) {
        return false
      }

      if (busy) {
        // Promote to the head, then interrupt. The gateway always emits a
        // settle (message.complete + session.info running:false) when the
        // turn unwinds, and the busy→false auto-drain below sends this entry.
        promoteQueuedPrompt(activeQueueSessionKey, id)
        triggerHaptic('selection')
        void Promise.resolve(onCancel())

        return true
      }

      // A manual send clears the auto-drain backoff so a stuck entry the user
      // taps gets a fresh attempt (and re-enables auto-retry on success).
      drainFailuresRef.current.delete(id)

      // An explicit tap must not be silently swallowed by a race: WAIT for an
      // in-flight drain elsewhere (bounded), and say so if it outlasts us —
      // the entry stays queued either way.
      const outcome = await runDrain(entries => entries.find(e => e.id === id), {
        timeoutMs: MANUAL_SEND_WAIT_MS,
        wait: true
      })

      if (outcome === 'contended') {
        notify({
          id: 'composer-queue-busy-elsewhere',
          kind: 'info',
          title: t.composer.queueBusyElsewhereTitle,
          message: t.composer.queueBusyElsewhereBody
        })
      }

      return outcome === 'sent'
    },
    [activeQueueSessionKey, busy, onCancel, queueEdit, runDrain, t]
  )

  // Edge-independent auto-drain: send the head whenever the session is idle and
  // the queue is non-empty, bounding retries so a thrown/rejected onSubmit (e.g.
  // a stale-session 404) can't strand the entry permanently nor spin-loop. The
  // drain locks serialize sends; a remount/reconnect resets the failure counts.
  const autoDrainNext = useCallback(() => {
    if (!mountedRef.current || busy || drainingQueueRef.current || !activeQueueSessionKey) {
      return
    }

    const entry = pickDrainHead(queuedPrompts)

    if (!entry || (drainFailuresRef.current.get(entry.id) ?? 0) >= MAX_AUTO_DRAIN_ATTEMPTS) {
      return
    }

    // The effect below only re-fires when its deps change, and neither a
    // rejected send nor a lost claim changes them — each non-sent outcome must
    // therefore schedule its own wake-up or the entry strands in an idle
    // window (and MAX_AUTO_DRAIN_ATTEMPTS could never actually be reached).
    const scheduleRetry = (attempt: number) => {
      if (retryTimerRef.current !== null) {
        return
      }

      retryTimerRef.current = setTimeout(() => {
        retryTimerRef.current = null
        autoDrainRef.current()
      }, AUTO_DRAIN_RETRY_BASE_MS * attempt)
    }

    const onFail = () => {
      const fails = (drainFailuresRef.current.get(entry.id) ?? 0) + 1
      drainFailuresRef.current.set(entry.id, fails)

      if (fails >= MAX_AUTO_DRAIN_ATTEMPTS) {
        notify({
          id: 'composer-queue-stuck',
          kind: 'error',
          title: t.composer.queueStuckTitle,
          message: t.composer.queueStuckBody
        })
      } else {
        scheduleRetry(fails)
      }
    }

    // A lost cross-window race needs a different wake-up than a failed send:
    // the winner only writes storage (and thus emits the event that re-runs
    // us) when it SENDS. A winner that gets rejected — or whose window dies
    // mid-submit — leaves no trace, so wait for its claim to release (the
    // browser frees a dead window's locks) and re-check then. One waiter at a
    // time is enough: it re-enters this function, which re-arms if still needed.
    const onContended = () => {
      if (claimWaiterArmedRef.current) {
        return
      }

      claimWaiterArmedRef.current = true

      void whenSessionDrainClaimReleased(activeQueueSessionKey).then(() => {
        claimWaiterArmedRef.current = false
        autoDrainRef.current()
      })
    }

    // Re-locate the entry by id rather than submitting the captured object:
    // runDrain picks from the fresh store read inside the drain claim, so an
    // entry another window drained meanwhile simply isn't found ('empty').
    // Only a rejected send counts toward the retry cap — burning attempts on
    // 'empty'/'contended' (races this window lost) would strand a healthy
    // entry and raise the stuck-queue toast spuriously.
    void runDrain(entries => entries.find(candidate => candidate.id === entry.id))
      .then(outcome => {
        if (outcome === 'rejected') {
          onFail()
        } else if (outcome === 'contended') {
          onContended()
        }
      })
      .catch(onFail)
  }, [activeQueueSessionKey, busy, pickDrainHead, queuedPrompts, runDrain, t])

  // Keep the liveness wake-ups (retry timer, claim waiter) pointing at the
  // latest closure, and never let a timer outlive the composer.
  autoDrainRef.current = autoDrainNext

  useEffect(() => {
    mountedRef.current = true

    return () => {
      mountedRef.current = false

      if (retryTimerRef.current !== null) {
        clearTimeout(retryTimerRef.current)
        retryTimerRef.current = null
      }
    }
  }, [])

  // Re-key on a runtime session-id change. A stable stored id (queueSessionKey)
  // never churns, so a change there is a real session switch and must NOT
  // migrate; only the runtime-derived key (queueSessionKey falsy → key is
  // sessionId) churns on a backend bounce/resume of the same conversation.
  useEffect(() => {
    const prev = prevQueueKeyRef.current
    prevQueueKeyRef.current = activeQueueSessionKey

    if (queueSessionKey || !prev || !activeQueueSessionKey || prev === activeQueueSessionKey) {
      return
    }

    void migrateQueuedPrompts(prev, activeQueueSessionKey)
  }, [activeQueueSessionKey, queueSessionKey])

  // Queued turns flow whenever the session is idle — on the busy→false settle
  // edge, on mount/reconnect, and after a re-key — so a swallowed edge can't
  // strand them. To cancel queued turns, the user deletes them from the panel.
  useEffect(() => {
    if (shouldAutoDrain({ isBusy: busy, queueLength: queuedPrompts.length })) {
      autoDrainNext()
    }
  }, [autoDrainNext, busy, queuedPrompts.length])

  // Queue-edit cleanup: on session swap the scope effect already stashed the
  // edit snapshot; only restore into the composer when still on the same scope.
  useEffect(() => {
    if (!queueEdit) {
      return
    }

    if (queueEdit.sessionKey === activeQueueSessionKey) {
      if (editingQueuedPrompt) {
        return
      }

      setQueueEditSnapshot(null)
      loadIntoComposer(queueEdit.draft, queueEdit.attachments)

      return
    }

    setQueueEditSnapshot(null)
  }, [activeQueueSessionKey, editingQueuedPrompt, queueEdit, setQueueEditSnapshot]) // eslint-disable-line react-hooks/exhaustive-deps

  return {
    beginQueuedEdit,
    drainNextQueued,
    editingQueuedPrompt,
    exitQueuedEdit,
    queueCurrentDraft,
    queueEdit,
    queuedPrompts,
    sendQueuedNow,
    stepQueuedEdit
  }
}
