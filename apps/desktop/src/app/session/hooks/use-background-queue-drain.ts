import { useStore } from '@nanostores/react'
import { type MutableRefObject, useCallback, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { resetBrowseState } from '@/store/composer-input-history'
import {
  $parkedQueueSessions,
  $queuedPromptsBySession,
  getLatestQueuedPrompts,
  MAX_AUTO_DRAIN_ATTEMPTS,
  type QueuedPromptEntry,
  removeQueuedPrompt,
  shouldAutoDrain
} from '@/store/composer-queue'
import { beginComposerQueueDrain, finishComposerQueueDrain } from '@/store/composer-queue-drain'
import {
  type ComposerStorageOwner,
  decodeComposerStorageScopeKey,
  normalizeComposerStorageOwner,
  resolveComposerStorageScopeKey
} from '@/store/composer-storage-scope'
import { notify } from '@/store/notifications'
import { $sessions, idsShareLineage } from '@/store/session'
import { $workingSessionIds } from '@/store/session-states'

import type { SubmitTextOptions } from './use-prompt-actions/utils'

type SubmitQueuedPrompt = (text: string, options?: SubmitTextOptions) => Promise<boolean> | boolean

interface BackgroundQueueDrainOptions {
  enabled: boolean
  owner: ComposerStorageOwner
  runtimeIdByStoredSessionIdRef: MutableRefObject<Map<string, string>>
  selectedStoredSessionId: string | null
  submitText: SubmitQueuedPrompt
}

const BACKGROUND_DRAIN_RETRY_MS = 750

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
  owner,
  runtimeIdByStoredSessionIdRef,
  selectedStoredSessionId,
  submitText
}: BackgroundQueueDrainOptions) {
  const { t } = useI18n()
  const queuedPromptsBySession = useStore($queuedPromptsBySession)
  const parkedQueueSessions = useStore($parkedQueueSessions)
  const workingSessionIds = useStore($workingSessionIds)
  const submitTextRef = useRef(submitText)
  const drainFailuresRef = useRef(new Map<string, number>())
  const retryTimersRef = useRef<number[]>([])
  const [retryTick, setRetryTick] = useState(0)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    submitTextRef.current = submitText
  }, [submitText])

  const scheduleRetry = useCallback(() => {
    if (typeof window === 'undefined') {
      return
    }

    const timer = window.setTimeout(() => {
      retryTimersRef.current = retryTimersRef.current.filter(id => id !== timer)
      setRetryTick(tick => tick + 1)
    }, BACKGROUND_DRAIN_RETRY_MS)

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
    (sessionKey: string, storedSessionId: string | null, entry: QueuedPromptEntry) => {
      const drain = beginComposerQueueDrain(sessionKey, entry.id)

      if (!drain) {
        return
      }

      const onFail = () => {
        const failures = (drainFailuresRef.current.get(entry.id) ?? 0) + 1
        drainFailuresRef.current.set(entry.id, failures)

        if (failures >= MAX_AUTO_DRAIN_ATTEMPTS) {
          notify({
            id: `composer-background-queue-stuck-${sessionKey}`,
            kind: 'error',
            title: t.composer.queueStuckTitle,
            message: t.composer.queueStuckBody
          })

          return
        }

        scheduleRetry()
      }
      void (async () => {
        let acceptedEntry: QueuedPromptEntry | null = null
        let failed = false
        let runtimeSessionId: string | null = null

        try {
          // A migration can hand the lock to another qualified key before this
          // microtask runs. Resolve that scope, then match the entry only inside
          // its exact owner namespace: entry ids are not an ownership boundary.
          const liveScopeKey = resolveComposerStorageScopeKey(sessionKey)
          const liveEntry = getLatestQueuedPrompts(liveScopeKey).find(candidate => candidate.id === entry.id)

          if (!liveEntry) {
            return
          }

          runtimeSessionId = storedSessionId
            ? (runtimeIdByStoredSessionIdRef.current.get(storedSessionId) ?? null)
            : null

          const accepted = await Promise.resolve(
            submitTextRef.current(liveEntry.text, {
              attachments: liveEntry.attachments,
              fromQueue: true,
              sessionId: runtimeSessionId,
              storedSessionId
            })
          )

          if (accepted === false) {
            failed = true

            return
          }

          acceptedEntry = liveEntry
        } catch {
          failed = true
        } finally {
          const settledKey = finishComposerQueueDrain(drain) ?? sessionKey

          if (acceptedEntry) {
            drainFailuresRef.current.delete(acceptedEntry.id)
            removeQueuedPrompt(settledKey, acceptedEntry.id)
            resetBrowseState(runtimeSessionId)
          } else if (failed) {
            onFail()
          }
        }
      })()
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
    const normalizedOwner = normalizeComposerStorageOwner(owner)

    for (const [sessionKey, entries] of Object.entries(queuedPromptsBySession)) {
      const target = decodeComposerStorageScopeKey(sessionKey)

      if (
        !target ||
        target.owner.connectionId !== normalizedOwner.connectionId ||
        target.owner.profile !== normalizedOwner.profile
      ) {
        continue
      }

      const storedSessionId = target.storedSessionId

      const isSelected =
        storedSessionId === null
          ? selectedStoredSessionId === null
          : Boolean(selectedStoredSessionId) && idsShareLineage(storedSessionId, selectedStoredSessionId!, sessions)

      const isBusy = Boolean(
        storedSessionId && working.some(workingId => idsShareLineage(storedSessionId, workingId, sessions))
      )

      if (
        isSelected ||
        !shouldAutoDrain({
          isBusy,
          parked: Boolean(parkedQueueSessions[sessionKey]),
          queueLength: entries.length
        })
      ) {
        continue
      }

      const entry = entries[0]

      if (!entry || (drainFailuresRef.current.get(entry.id) ?? 0) >= MAX_AUTO_DRAIN_ATTEMPTS) {
        continue
      }

      drainSessionQueue(sessionKey, storedSessionId, entry)
    }
  }, [
    drainSessionQueue,
    enabled,
    owner,
    parkedQueueSessions,
    queuedPromptsBySession,
    retryTick,
    selectedStoredSessionId,
    workingSessionIds
  ])
}
