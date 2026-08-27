import { useStore } from '@nanostores/react'
import { type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { useSessionSlice } from '@/lib/use-session-slice'
import { type ComposerAttachment } from '@/store/composer'
import { resetBrowseState } from '@/store/composer-input-history'
import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  enqueueQueuedPrompt,
  getLatestQueuedPrompts,
  getQueuedPrompts,
  isSteerableEntry,
  MAX_AUTO_DRAIN_ATTEMPTS,
  migrateQueuedPrompts,
  promoteQueuedPrompt,
  type QueuedPromptEntry,
  removeQueuedPrompt,
  shouldAutoDrain,
  unparkQueuedPrompts,
  updateQueuedPrompt
} from '@/store/composer-queue'
import {
  beginComposerQueueDrain,
  finishComposerQueueDrain,
  isComposerQueueDrainExcluded
} from '@/store/composer-queue-drain'
import { migrateComposerStorageScope } from '@/store/composer-storage-migration'
import { notify } from '@/store/notifications'

import { cloneAttachments, type QueueEditState } from '../composer-utils'
import { useComposerScope } from '../scope'
import type { ChatBarProps } from '../types'

interface UseComposerQueueArgs {
  actionsDisabled: boolean
  activeQueueSessionKey: string | null
  attachments: ComposerAttachment[]
  busy: boolean
  clearDraft: () => void
  draftRef: RefObject<string>
  focusInput: () => void
  loadIntoComposer: (text: string, attachments: ComposerAttachment[]) => void
  onCancel: ChatBarProps['onCancel']
  onSteer: ChatBarProps['onSteer']
  onSubmit: ChatBarProps['onSubmit']
  queueEditRef: RefObject<QueueEditState | null>
  queueSessionKey: ChatBarProps['queueSessionKey']
  sessionId: string | null | undefined
  submitScopeKey: string | null
}

/**
 * The composer's queue engine — everything about queued turns: the per-session
 * queue store binding, in-place queued-prompt editing (begin/step/exit), the
 * shared drain lock + send-then-remove sequence, manual send-now, and the
 * edge-independent auto-drain with bounded retries. It consumes the draft API
 * (draftRef/clearDraft/loadIntoComposer/focusInput) and writes the
 * coordinator-owned `queueEditRef` so the draft engine can read the edit state
 * without a back-reference. Behaviour-identical to the inline original.
 */
export function useComposerQueue({
  actionsDisabled,
  activeQueueSessionKey,
  attachments,
  busy,
  clearDraft,
  draftRef,
  focusInput,
  loadIntoComposer,
  onCancel,
  onSteer,
  onSubmit,
  queueEditRef,
  queueSessionKey,
  sessionId,
  submitScopeKey
}: UseComposerQueueArgs) {
  const { t } = useI18n()
  const scope = useComposerScope()

  // Per-session slice (edge): re-renders only when THIS session's queue changes,
  // not on cross-session queue churn (the plain atom's map ref changes on every
  // write; the keyed array does not).
  const queuedPrompts = useSessionSlice($queuedPromptsBySession, activeQueueSessionKey)

  // Parked = the user explicitly halted this session (Stop/Esc) while prompts
  // were queued. The map is tiny (only halted sessions) so a plain subscribe
  // is fine; the auto-drain effect below reads it as a gate.
  const parkedSessions = useStore($parkedQueueSessions)
  const queueParked = Boolean(activeQueueSessionKey && parkedSessions[activeQueueSessionKey])

  const [queueEdit, setQueueEdit] = useState<QueueEditState | null>(null)

  useLayoutEffect(() => {
    queueEditRef.current = queueEdit
  }, [queueEdit, queueEditRef])

  const setQueueEditSnapshot = useCallback(
    (next: QueueEditState | null) => {
      queueEditRef.current = next
      setQueueEdit(next)
    },
    [queueEditRef]
  )

  const editingQueuedPrompt = queueEdit ? (queuedPrompts.find(entry => entry.id === queueEdit.entryId) ?? null) : null

  const prevQueueKeyRef = useRef(activeQueueSessionKey)
  const drainFailuresRef = useRef(new Map<string, number>())
  const [autoDrainRetryEpoch, setAutoDrainRetryEpoch] = useState(0)

  const beginQueuedEdit = (entry: QueuedPromptEntry) => {
    if (
      actionsDisabled ||
      !activeQueueSessionKey ||
      queueEdit ||
      isComposerQueueDrainExcluded(activeQueueSessionKey, entry.id)
    ) {
      return
    }

    setQueueEditSnapshot({
      attachments: cloneAttachments(attachments),
      draft: draftRef.current,
      entryId: entry.id,
      sessionKey: activeQueueSessionKey
    })
    // Edit what the panel SHOWS. A queued `/skill` entry's text is the
    // expanded skill body — never drop that into the composer.
    loadIntoComposer(entry.displayText ?? entry.text, entry.attachments)
    triggerHaptic('selection')
    focusInput()
  }

  // Walk queued entries while editing (ArrowUp = older, ArrowDown = newer),
  // saving the in-progress edit on each step. Stepping newer past the last
  // entry exits edit mode and restores the pre-edit draft.
  const stepQueuedEdit = (direction: -1 | 1) => {
    if (actionsDisabled || !queueEdit) {
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
      loadIntoComposer(next.displayText ?? next.text, next.attachments)
    } else {
      setQueueEditSnapshot(null)
      loadIntoComposer(queueEdit.draft, queueEdit.attachments)
    }

    triggerHaptic(saved ? 'success' : 'selection')
    focusInput()

    return true
  }

  const exitQueuedEdit = (action: 'cancel' | 'save'): boolean => {
    if (actionsDisabled || !queueEdit) {
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

    if (actionsDisabled || !activeQueueSessionKey || (!text.trim() && attachments.length === 0)) {
      return false
    }

    if (!enqueueQueuedPrompt(activeQueueSessionKey, { text, attachments })) {
      return false
    }

    clearDraft()
    scope.attachments.clear()
    triggerHaptic('selection')

    return true
  }, [actionsDisabled, activeQueueSessionKey, attachments, clearDraft, draftRef, scope.attachments])

  // All queue drain paths share one lock + send-then-remove sequence.
  // `pickEntry` lets each caller choose head, by-id, or skip-edited.
  const runDrain = useCallback(
    async (pickEntry: (entries: QueuedPromptEntry[]) => QueuedPromptEntry | undefined): Promise<boolean> => {
      if (actionsDisabled || !activeQueueSessionKey) {
        return false
      }

      const drainQueueSessionKey = activeQueueSessionKey
      const drainRuntimeSessionId = sessionId ?? null
      const entry = pickEntry(getQueuedPrompts(drainQueueSessionKey))

      if (!entry) {
        return false
      }

      const drain = beginComposerQueueDrain(drainQueueSessionKey, entry.id)

      if (!drain) {
        return false
      }

      const liveEntry = getLatestQueuedPrompts(drainQueueSessionKey).find(candidate => candidate.id === entry.id)

      if (!liveEntry) {
        finishComposerQueueDrain(drain)

        return false
      }

      let drainFinished = false

      try {
        const accepted = await Promise.resolve(
          onSubmit(liveEntry.text, {
            attachments: liveEntry.attachments,
            ...(liveEntry.displayText ? { displayText: liveEntry.displayText } : {}),
            fromQueue: true,
            sessionId: drainRuntimeSessionId,
            storedSessionId: submitScopeKey
          })
        )

        if (accepted === false) {
          return false
        }

        const settledQueueSessionKey = finishComposerQueueDrain(drain) ?? drainQueueSessionKey

        drainFinished = true

        drainFailuresRef.current.delete(liveEntry.id)
        removeQueuedPrompt(settledQueueSessionKey, liveEntry.id)
        resetBrowseState(drainRuntimeSessionId)

        return true
      } finally {
        if (!drainFinished) {
          finishComposerQueueDrain(drain)
        }
      }
    },
    [actionsDisabled, activeQueueSessionKey, onSubmit, sessionId, submitScopeKey]
  )

  const pickDrainHead = useCallback(
    (entries: QueuedPromptEntry[]) => {
      const skip = queueEditRef.current?.entryId

      return skip ? entries.find(e => e.id !== skip) : entries[0]
    },
    [queueEditRef] // reads the edit id off a ref so the lock-holder always sees the latest
  )

  const drainNextQueued = useCallback(() => {
    if (activeQueueSessionKey) {
      unparkQueuedPrompts(activeQueueSessionKey)
    }

    return runDrain(pickDrainHead)
  }, [activeQueueSessionKey, pickDrainHead, runDrain])

  const sendQueuedNow = useCallback(
    (id: string) => {
      if (actionsDisabled || !activeQueueSessionKey || id === queueEdit?.entryId) {
        return false
      }

      if (busy) {
        // Promote to the head, then interrupt. The gateway always emits a
        // settle (message.complete + session.info running:false) when the
        // turn unwinds, and the busy→false auto-drain below sends this entry.
        // Unpark first: this interrupt exists to REACH the queue, so the
        // settle drain must flow — unlike a Stop/Esc halt, which parks.
        promoteQueuedPrompt(activeQueueSessionKey, id)
        unparkQueuedPrompts(activeQueueSessionKey)
        triggerHaptic('selection')
        void Promise.resolve(onCancel())

        return true
      }

      // A manual send clears the auto-drain backoff so a stuck entry the user
      // taps gets a fresh attempt (and re-enables auto-retry on success).
      drainFailuresRef.current.delete(id)
      unparkQueuedPrompts(activeQueueSessionKey)

      return runDrain(entries => entries.find(e => e.id === id))
    },
    [actionsDisabled, activeQueueSessionKey, busy, onCancel, queueEdit, runDrain]
  )

  // Deliver a queued entry as a mid-turn redirect — the queue-panel sibling of
  // the composer's steer-on-Enter. No interrupt, no drain lock: a redirect
  // rides the live turn (the gateway either restarts the active request with
  // its displayed context or waits for the current tool boundary), so the turn
  // keeps flowing and the remaining queue is untouched. Only meaningful while
  // busy — idle has no turn to redirect, and `sendQueuedNow` already covers it.
  const steerQueuedNow = useCallback(
    async (id: string): Promise<boolean> => {
      if (actionsDisabled || !onSteer || !busy || !activeQueueSessionKey || id === queueEditRef.current?.entryId) {
        return false
      }

      const entry = getQueuedPrompts(activeQueueSessionKey).find(e => e.id === id)

      if (!entry || !isSteerableEntry(entry)) {
        return false
      }

      const steerScopeKey = activeQueueSessionKey
      const drain = beginComposerQueueDrain(steerScopeKey, entry.id)

      if (!drain) {
        return false
      }

      const liveEntry = getLatestQueuedPrompts(steerScopeKey).find(candidate => candidate.id === entry.id)

      if (!liveEntry || !isSteerableEntry(liveEntry)) {
        finishComposerQueueDrain(drain)

        return false
      }

      let drainFinished = false

      triggerHaptic('submit')
      // This click is the manual resume gesture. Lift an older park now; a Stop
      // pressed while the redirect is pending establishes a newer park that
      // settlement must not clear.
      unparkQueuedPrompts(steerScopeKey)

      try {
        const accepted = await Promise.resolve(onSteer(liveEntry.text))

        // Rejected (turn already settling, gateway said no): leave the entry
        // queued exactly where it was — the settle drain picks it up, so the
        // words are never lost. Only a delivered redirect consumes the entry.
        if (!accepted) {
          return false
        }

        const settledScopeKey = finishComposerQueueDrain(drain) ?? steerScopeKey

        drainFinished = true
        drainFailuresRef.current.delete(liveEntry.id)
        removeQueuedPrompt(settledScopeKey, liveEntry.id)

        return true
      } finally {
        if (!drainFinished) {
          finishComposerQueueDrain(drain)
        }
      }
    },
    [actionsDisabled, activeQueueSessionKey, busy, onSteer, queueEditRef]
  )

  // Edge-independent auto-drain: send the head whenever the session is idle and
  // the queue is non-empty, bounding retries so a thrown/rejected onSubmit (e.g.
  // a stale-session 404) can't strand the entry permanently nor spin-loop. The
  // drain lock serializes sends; a remount/reconnect resets the failure counts.
  const autoDrainNext = useCallback(() => {
    if (actionsDisabled || busy || queueParked || !activeQueueSessionKey) {
      return
    }

    const entry = pickDrainHead(queuedPrompts)

    if (
      !entry ||
      isComposerQueueDrainExcluded(activeQueueSessionKey, entry.id) ||
      (drainFailuresRef.current.get(entry.id) ?? 0) >= MAX_AUTO_DRAIN_ATTEMPTS
    ) {
      return
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
      }
    }

    void runDrain(() => entry)
      .then(sent => {
        if (!sent) {
          onFail()
          setAutoDrainRetryEpoch(epoch => epoch + 1)
        }
      })
      .catch(() => {
        onFail()
        setAutoDrainRetryEpoch(epoch => epoch + 1)
      })
  }, [actionsDisabled, activeQueueSessionKey, busy, pickDrainHead, queueParked, queuedPrompts, runDrain, t])

  // Re-key on a runtime session-id change. A stable stored id (queueSessionKey)
  // never churns, so a change there is a real session switch and must NOT
  // migrate; only the runtime-derived key (queueSessionKey falsy → key is
  // sessionId) churns on a backend bounce/resume of the same conversation.
  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    const prev = prevQueueKeyRef.current
    prevQueueKeyRef.current = activeQueueSessionKey

    if (!prev || !activeQueueSessionKey || prev === activeQueueSessionKey) {
      return
    }

    if (queueSessionKey === undefined) {
      if (!migrateComposerStorageScope(prev, activeQueueSessionKey)) {
        migrateQueuedPrompts(prev, activeQueueSessionKey)
      }
    }
  }, [activeQueueSessionKey, queueSessionKey])

  // Queued turns flow whenever the session is idle — on the busy→false settle
  // edge, on mount/reconnect, and after a re-key — so a swallowed edge can't
  // strand them. A park (explicit Stop/Esc) is the one gate: those entries wait
  // for the user. To cancel queued turns, the user deletes them from the panel.
  useEffect(() => {
    if (shouldAutoDrain({ isBusy: busy, parked: queueParked, queueLength: queuedPrompts.length })) {
      autoDrainNext()
    }
  }, [autoDrainNext, autoDrainRetryEpoch, busy, queueParked, queuedPrompts.length])

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
    queueParked,
    queuedPrompts,
    sendQueuedNow,
    steerQueuedNow,
    stepQueuedEdit
  }
}
