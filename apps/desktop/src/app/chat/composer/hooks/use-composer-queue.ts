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
  isQueuedPromptSentElsewhere,
  markQueuedPromptSent,
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
  // sends), one pending claim-release waiter per session key (lost
  // cross-window races), a settle promise waiting manual sends can queue on,
  // and refs to the latest autoDrainNext / busy so every wake-up and every
  // in-claim decision sees fresh state, not a stale closure. The mounted flag
  // stops a late-firing timer/waiter from draining for an unmounted composer.
  const retryTimerRef = useRef<null | ReturnType<typeof setTimeout>>(null)
  const claimWaiterKeyRef = useRef<null | string>(null)
  const drainSettledRef = useRef<null | Promise<void>>(null)
  const migrationChainRef = useRef<Promise<unknown>>(Promise.resolve())
  const autoDrainRef = useRef<() => void>(() => {})
  const busyRef = useRef(busy)
  const mountedRef = useRef(true)

  busyRef.current = busy

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
  // by-id, or skip-edited; the result reports an outcome (not a bare boolean)
  // so auto-drain counts only genuine send failures toward its retry cap, plus
  // the id actually attempted so the count lands on the right entry.
  const runDrain = useCallback(
    async (
      pickEntry: (entries: QueuedPromptEntry[]) => QueuedPromptEntry | undefined,
      claim: { wait?: boolean; timeoutMs?: number } = {}
    ): Promise<{ entryId: null | string; outcome: 'busy' | 'contended' | 'empty' | 'rejected' | 'sent' }> => {
      if (!activeQueueSessionKey) {
        return { entryId: null, outcome: 'contended' }
      }

      if (drainingQueueRef.current) {
        if (!claim.wait) {
          return { entryId: null, outcome: 'contended' }
        }

        // A waiting caller (manual send) must not be bounced by THIS window's
        // own in-flight drain — that is local contention, not "another
        // window". Wait for the local lane to clear, inside the same budget:
        // the settle promise is raced against the remaining time so a hung
        // in-flight drain cannot suspend the deadline check itself.
        const deadline = claim.timeoutMs === undefined ? Infinity : Date.now() + claim.timeoutMs

        while (drainingQueueRef.current) {
          const remaining = deadline - Date.now()

          if (remaining <= 0) {
            return { entryId: null, outcome: 'contended' }
          }

          const settled = drainSettledRef.current ?? Promise.resolve()

          await (remaining === Infinity
            ? settled
            : Promise.race([settled, new Promise(resolve => setTimeout(resolve, remaining))]))
        }

        if (claim.timeoutMs !== undefined) {
          claim = { ...claim, timeoutMs: Math.max(1, deadline - Date.now()) }
        }
      }

      drainingQueueRef.current = true
      let settleDrain!: () => void
      drainSettledRef.current = new Promise<void>(resolve => (settleDrain = resolve))
      let attemptedId: null | string = null

      try {
        const outcome = await withSessionDrainClaim(
          activeQueueSessionKey,
          async (): Promise<'busy' | 'empty' | 'rejected' | 'sent'> => {
            // The session may have started a turn while we waited for the
            // lane or the claim (typically: the very drain we waited on got
            // accepted). Submitting now would just bounce off the gateway;
            // the caller decides what busy means for its flow.
            if (busyRef.current) {
              return 'busy'
            }

            // Pick INSIDE the claim, and from the fresh store read rather than
            // the rendered slice or the atom: the storage event that syncs them
            // is asynchronous, so only the fresh read reflects a removal
            // another window just made. Picking any earlier (or from anything
            // staler) is what allowed the double submit.
            const entry = pickEntry(readFreshQueuedPrompts(activeQueueSessionKey))

            if (!entry) {
              return 'empty'
            }

            attemptedId = entry.id

            // Second, storage-independent sent check: a window whose removals
            // cannot reach storage (quota) marks its sends with a held Web
            // Lock instead. Purge rather than resubmit.
            if (await isQueuedPromptSentElsewhere(entry.id)) {
              removeQueuedPrompt(activeQueueSessionKey, entry.id)

              return 'empty'
            }

            let accepted: boolean | void

            try {
              accepted = await Promise.resolve(
                onSubmit(entry.text, { attachments: entry.attachments, fromQueue: true })
              )
            } catch {
              // A thrown submit is a rejection of THIS entry — reporting it
              // through the outcome keeps the failure count attributed to the
              // entry actually attempted, not a pre-claim guess.
              return 'rejected'
            }

            if (accepted === false) {
              return 'rejected'
            }

            drainFailuresRef.current.delete(entry.id)
            markQueuedPromptSent(entry.id)
            removeQueuedPrompt(activeQueueSessionKey, entry.id)
            resetBrowseState(sessionId)

            return 'sent'
          },
          claim
        )

        // null = the claim was unavailable (or the wait timed out); whoever
        // holds it owns the entry for now.
        return { entryId: attemptedId, outcome: outcome ?? 'contended' }
      } catch {
        // Lock-manager failure. Post-pick it is a rejection of the attempted
        // entry; pre-pick it is indistinguishable from contention.
        return { entryId: attemptedId, outcome: attemptedId ? 'rejected' : 'contended' }
      } finally {
        drainingQueueRef.current = false
        settleDrain()
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
    () => runDrain(pickDrainHead).then(({ outcome }) => outcome === 'sent'),
    [pickDrainHead, runDrain]
  )

  // Send-now while a turn runs: promote to the head, then interrupt. The
  // gateway always emits a settle (message.complete + session.info
  // running:false) when the turn unwinds, and the busy→false auto-drain
  // sends the promoted entry.
  const promoteAndInterrupt = useCallback(
    (id: string) => {
      promoteQueuedPrompt(activeQueueSessionKey, id)
      triggerHaptic('selection')
      void Promise.resolve(onCancel())

      return true
    },
    [activeQueueSessionKey, onCancel]
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
        return promoteAndInterrupt(id)
      }

      // A manual send clears the auto-drain backoff so a stuck entry the user
      // taps gets a fresh attempt (and re-enables auto-retry on success).
      drainFailuresRef.current.delete(id)

      // An explicit tap must not be silently swallowed by a race: WAIT for an
      // in-flight drain (this window's or another's, bounded), and say so if
      // it outlasts us — the entry stays queued either way.
      const { outcome } = await runDrain(entries => entries.find(e => e.id === id), {
        timeoutMs: MANUAL_SEND_WAIT_MS,
        wait: true
      })

      // A turn started while we waited (usually the very drain we waited on
      // being accepted): the tap now means what a tap-during-busy means.
      if (outcome === 'busy' && readFreshQueuedPrompts(activeQueueSessionKey).some(e => e.id === id)) {
        return promoteAndInterrupt(id)
      }

      if (outcome === 'contended') {
        notify({
          id: 'composer-queue-busy-elsewhere',
          kind: 'info',
          title: t.composer.queueBusyElsewhereTitle,
          message: t.composer.queueBusyElsewhereBody
        })
      }

      if (outcome !== 'sent') {
        // The tap consumed no auto-drain wake-up, but the queue may still have
        // drainable entries whose wake-ups landed during our in-flight window;
        // one bounded re-check keeps the lane live.
        setTimeout(() => autoDrainRef.current(), 0)
      }

      return outcome === 'sent'
    },
    [activeQueueSessionKey, busy, promoteAndInterrupt, queueEdit, runDrain, t]
  )

  // Edge-independent auto-drain: send the head whenever the session is idle and
  // the queue is non-empty, bounding retries so a thrown/rejected onSubmit (e.g.
  // a stale-session 404) can't strand the entry permanently nor spin-loop. The
  // drain locks serialize sends; a remount/reconnect resets the failure counts.
  const autoDrainNext = useCallback(() => {
    if (!mountedRef.current || busy || !activeQueueSessionKey) {
      return
    }

    if (drainingQueueRef.current) {
      // A drain is already in flight in this window; drop this wake-up. Every
      // outcome of the in-flight drain produces its own follow-up signal —
      // 'sent' writes storage (event → effect), 'rejected' schedules a
      // backoff retry, 'contended' arms the claim waiter, manual sends kick a
      // re-check when they finish — so nothing is lost by not replaying.
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

    const onFail = (id: string) => {
      const fails = (drainFailuresRef.current.get(id) ?? 0) + 1
      drainFailuresRef.current.set(id, fails)

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
    // browser frees a dead window's locks) and re-check then. One waiter per
    // session key is enough (keyed, so a stale waiter on a previous session
    // can never suppress the current one): it re-enters this function, which
    // re-arms if still needed.
    const onContended = () => {
      const key = activeQueueSessionKey

      if (claimWaiterKeyRef.current === key) {
        return
      }

      claimWaiterKeyRef.current = key

      void whenSessionDrainClaimReleased(key).then(waited => {
        if (claimWaiterKeyRef.current === key) {
          claimWaiterKeyRef.current = null
        }

        // Only a genuine wait-and-release is a wake-up; re-running on a
        // failed wait would spin arm→fail→re-arm with no progress.
        if (waited) {
          autoDrainRef.current()
        }
      })
    }

    // Re-pick the head INSIDE the claim from fresh state (not the captured
    // entry object): another window's removal, edit, or PROMOTE must all be
    // honored at send time. The cap guard rides along in the picker as a
    // defensive backstop for rendered-vs-fresh skew (tombstones make that
    // skew nearly unreachable, so the pre-check above is the pinned path).
    // Failure counts land on whatever entry was actually attempted — runDrain
    // converts thrown submits into 'rejected' with the attempted id, so only
    // genuine send failures burn attempts; 'busy' waits for the busy→false
    // effect edge, 'empty'/'contended' are races this window lost.
    void runDrain(entries => {
      const candidate = pickDrainHead(entries)

      if (!candidate || (drainFailuresRef.current.get(candidate.id) ?? 0) >= MAX_AUTO_DRAIN_ATTEMPTS) {
        return undefined
      }

      return candidate
    }).then(({ entryId, outcome }) => {
      if (outcome === 'rejected' && entryId) {
        onFail(entryId)
      } else if (outcome === 'contended') {
        onContended()
      }
    })
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
  // Migrations CHAIN: migrate waits (unbounded) on the source key's drain
  // claim, so two re-keys in quick succession must run K1→K2 before K2→K3 or
  // the second move reads K2 before the first lands and strands entries under
  // a dead intermediate key.
  useEffect(() => {
    const prev = prevQueueKeyRef.current
    prevQueueKeyRef.current = activeQueueSessionKey

    if (queueSessionKey || !prev || !activeQueueSessionKey || prev === activeQueueSessionKey) {
      return
    }

    const next = activeQueueSessionKey
    migrationChainRef.current = migrationChainRef.current
      .then(() => migrateQueuedPrompts(prev, next))
      .then(() => undefined)
      .catch(() => undefined)
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
