import { useStore } from '@nanostores/react'
import { type MutableRefObject, useCallback, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { sessionTitle } from '@/lib/chat-runtime'
import { resetBrowseState } from '@/store/composer-input-history'
import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  clearDrainFailure,
  getQueuedPrompts,
  hasExhaustedDrain,
  MAX_AUTO_DRAIN_ATTEMPTS,
  noteDrainFailure,
  noteQueueStuck,
  type QueuedPromptEntry,
  queueStuckNoticeId,
  recoverQueuedPrompts,
  removeQueuedPrompt,
  shouldAutoDrain
} from '@/store/composer-queue'
import { notify } from '@/store/notifications'
import { $sessions, idsShareLineage, sessionMatchesStoredId } from '@/store/session'
import { $workingSessionIds } from '@/store/session-states'

import type { SubmitTextOptions } from './use-prompt-actions/utils'

type SubmitQueuedPrompt = (text: string, options?: SubmitTextOptions) => Promise<boolean> | boolean

interface BackgroundQueueDrainOptions {
  enabled: boolean
  /** Bring a chat to the foreground — the shell owns `navigate`, so the alarm
   *  for a stuck background queue borrows it rather than routing itself. */
  onOpenSession?: (storedSessionId: string) => void
  runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>>
  selectedStoredSessionId: string | null
  submitText: SubmitQueuedPrompt
}

/**
 * How long to wait before each retry, by how many attempts have already failed.
 *
 * Was a flat 750ms, which spent the whole four-attempt budget in about three
 * seconds — shorter than a gateway bounce or a session resume, so an ordinary
 * hiccup was enough to declare the queue stuck and alarm the user. Backing off
 * gives the common transient time to clear before anyone is interrupted.
 *
 * One entry per RETRY, so MAX_AUTO_DRAIN_ATTEMPTS - 1 of them: the last failure
 * raises the alarm instead of scheduling anything. A fourth entry here would be
 * dead config that silently ignored every edit made to it.
 */
const BACKGROUND_DRAIN_RETRY_MS = [1_000, 4_000, 10_000]

/**
 * Drain queued prompts for sessions that are not currently rendered by ChatBar.
 *
 * The visible ChatBar owns the interactive queue panel for the selected session.
 * Without this background drain, a prompt queued in Session A can sit forever
 * after the user switches to Session B: the only auto-drain effect lives inside
 * the mounted ChatBar, so Session A's queue is not observed when A is offscreen.
 */
export function useBackgroundQueueDrain({
  enabled,
  onOpenSession,
  runtimeIdByStoredSessionIdRef,
  selectedStoredSessionId,
  submitText
}: BackgroundQueueDrainOptions) {
  const { t } = useI18n()
  const queuedPromptsBySession = useStore($queuedPromptsBySession)
  const parkedQueueSessions = useStore($parkedQueueSessions)
  const workingSessionIds = useStore($workingSessionIds)
  const submitTextRef = useRef(submitText)
  const openSessionRef = useRef(onOpenSession)
  const drainingSessionIdsRef = useRef(new Set<string>())
  const retryTimersRef = useRef<number[]>([])
  const [retryTick, setRetryTick] = useState(0)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    submitTextRef.current = submitText
    openSessionRef.current = onOpenSession
  }, [onOpenSession, submitText])

  const scheduleRetry = useCallback((failures: number) => {
    if (typeof window === 'undefined') {
      return
    }

    // `failures` is 1-based and never reaches MAX_AUTO_DRAIN_ATTEMPTS here, so
    // the clamp is belt-and-braces against a future caller, not live logic.
    const delay = BACKGROUND_DRAIN_RETRY_MS[Math.min(failures, BACKGROUND_DRAIN_RETRY_MS.length) - 1] ?? 1_000

    const timer = window.setTimeout(() => {
      retryTimersRef.current = retryTimersRef.current.filter(id => id !== timer)
      setRetryTick(tick => tick + 1)
    }, delay)

    retryTimersRef.current.push(timer)
  }, [])

  useEffect(
    () => () => {
      for (const timer of retryTimersRef.current) {
        window.clearTimeout(timer)
      }

      retryTimersRef.current = []
    },
    []
  )

  const drainSessionQueue = useCallback(
    (sessionKey: string, entry: QueuedPromptEntry) => {
      if (drainingSessionIdsRef.current.has(sessionKey)) {
        return
      }

      drainingSessionIdsRef.current.add(sessionKey)

      const onFail = () => {
        const failures = noteDrainFailure(entry.id)

        if (failures < MAX_AUTO_DRAIN_ATTEMPTS) {
          scheduleRetry(failures)

          return
        }

        // Classify only now, after four real failures, rather than gating the
        // drain on it: the sessions list loads asynchronously, so an early
        // "this chat does not exist" read would condemn perfectly live queues.
        const sessions = $sessions.get()

        const orphaned =
          sessions.length > 0 && !sessions.some(session => sessionMatchesStoredId(session, sessionKey))

        const pending = getQueuedPrompts(sessionKey)

        noteQueueStuck(sessionKey, entry.id)

        if (orphaned) {
          // Nothing will ever deliver these — the chat they were queued in is
          // gone, and no panel renders them either, so they are invisible as
          // well as undeliverable. Hand the words back instead of retrying.
          notify({
            action: {
              label: t.composer.queueLostAction,
              onClick: () => {
                const recovered = recoverQueuedPrompts(sessionKey)

                notify({
                  kind: recovered > 0 ? 'success' : 'info',
                  message: recovered > 0 ? t.composer.queueRecovered(recovered) : t.composer.queueRecoveredNothing
                })
              }
            },
            id: queueStuckNoticeId(sessionKey),
            kind: 'warning',
            message: t.composer.queueLostBody(pending.length),
            title: t.composer.queueLostTitle
          })

          return
        }

        const title = sessions.find(session => sessionMatchesStoredId(session, sessionKey))

        notify({
          ...(openSessionRef.current
            ? {
                action: {
                  label: t.composer.queueStuckAction,
                  onClick: () => openSessionRef.current?.(sessionKey)
                }
              }
            : {}),
          id: queueStuckNoticeId(sessionKey),
          kind: 'error',
          message: title ? t.composer.queueStuckBodyIn(sessionTitle(title)) : t.composer.queueStuckBody,
          title: t.composer.queueStuckTitle
        })
      }

      void Promise.resolve()
        .then(async () => {
          const liveEntry = getQueuedPrompts(sessionKey).find(candidate => candidate.id === entry.id)

          if (!liveEntry) {
            return true
          }

          const runtimeSessionId = runtimeIdByStoredSessionIdRef.current.get(sessionKey) ?? null

          const accepted = await Promise.resolve(
            submitTextRef.current(liveEntry.text, {
              attachments: liveEntry.attachments,
              fromQueue: true,
              sessionId: runtimeSessionId,
              storedSessionId: sessionKey
            })
          )

          if (accepted === false) {
            return false
          }

          clearDrainFailure(liveEntry.id)
          removeQueuedPrompt(sessionKey, liveEntry.id)
          resetBrowseState(runtimeSessionId)

          return true
        })
        .then(accepted => {
          if (!accepted) {
            onFail()
          }
        })
        .catch(onFail)
        .finally(() => {
          drainingSessionIdsRef.current.delete(sessionKey)
        })
    },
    [runtimeIdByStoredSessionIdRef, scheduleRetry, t]
  )

  useEffect(() => {
    if (!enabled) {
      return
    }

    // Queue keys prefer the lineage root (resolveComposerSessionKey) while
    // $workingSessionIds / selection may hold the compression tip. Strict
    // equality then mis-classifies a busy or selected chat as idle/offscreen.
    const sessions = $sessions.get()
    const working = [...workingSessionIds]

    for (const [sessionKey, entries] of Object.entries(queuedPromptsBySession)) {
      const isSelected =
        Boolean(selectedStoredSessionId) && idsShareLineage(sessionKey, selectedStoredSessionId!, sessions)

      const isBusy = working.some(workingId => idsShareLineage(sessionKey, workingId, sessions))

      if (
        isSelected ||
        drainingSessionIdsRef.current.has(sessionKey) ||
        !shouldAutoDrain({
          isBusy,
          parked: Boolean(parkedQueueSessions[sessionKey]),
          queueLength: entries.length
        })
      ) {
        continue
      }

      const entry = entries[0]

      if (!entry || hasExhaustedDrain(entry.id)) {
        continue
      }

      drainSessionQueue(sessionKey, entry)
    }
  }, [
    drainSessionQueue,
    enabled,
    parkedQueueSessions,
    queuedPromptsBySession,
    retryTick,
    selectedStoredSessionId,
    workingSessionIds
  ])
}
