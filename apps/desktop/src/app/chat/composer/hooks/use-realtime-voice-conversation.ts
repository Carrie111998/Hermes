import {
  CONSULT_TOOL_NAME,
  type RealtimeFunctionCall,
  type RealtimeTokenGrant,
  RealtimeVoiceClient,
  STEER_TOOL_NAME
} from '@hermes/shared'
import { useCallback, useEffect, useRef, useState } from 'react'

import { notifyError } from '@/store/notifications'

import type { ConversationStatus } from './use-voice-conversation'

/** How often the consult watcher checks whether the turn settled. */
const CONSULT_POLL_MS = 500
/** Mirror of the Python controller's function-output payload cap. */
const MAX_CONSULT_OUTPUT_CHARS = 6000
/** A consult with no running turn and no reply older than this is provably
 *  dead — fail it out so the session can accept new consults (mirrors the
 *  Python controller's stale-consult self-heal). */
const STALE_CONSULT_MIN_AGE_MS = 30_000

interface PendingVoiceResponse {
  id: string
  pending: boolean
  text: string
  /** Triggering user submission for this reply (null/absent = unknown). */
  userText?: string | null
}

interface RealtimeConversationOptions {
  busy: boolean
  enabled: boolean
  /** Mint an ephemeral grant (RPC `voice.realtime_token`). Fresh per dial —
   *  ephemeral credentials are never reused. Resolving null means realtime
   *  is unavailable and the surface is falling back (not a fatal error). */
  requestToken: () => Promise<RealtimeTokenGrant | null>
  /** Run a consult/steer task as a normal agent turn (no busy gate — the
   *  submit pipeline owns interrupt semantics). */
  submitTask: (text: string) => Promise<void>
  /** Interrupt the in-flight turn (Stop-button seam) — steering. */
  onInterrupt?: () => Promise<void> | void
  onFatalError?: () => void
  onStopWord?: () => void
  /** Whole-utterance stop-phrase check (mirrors voice.stop_phrases). */
  isStopWord: (text: string) => boolean
  pendingResponse: () => PendingVoiceResponse | null
  consumePendingResponse: () => void
  /** Awaited before the socket/mic opens (wake-word mic release). */
  beforeMicOpen?: () => Promise<void> | void
  failureLabel: string
}

interface TrackedConsult {
  callId: string
  task: string
  at: number
}

/**
 * The realtime (xAI S2S supervisor) voice conversation: grok-voice converses
 * with near-zero latency and delegates real work to Hermes through
 * consult/steer tool calls; Hermes turns run through the normal prompt
 * pipeline and their results are spoken as summaries. Exposes the same
 * surface as useVoiceConversation so the composer can swap loops.
 */
export function useRealtimeVoiceConversation({
  busy,
  enabled,
  requestToken,
  submitTask,
  onInterrupt,
  onFatalError,
  onStopWord,
  isStopWord,
  pendingResponse,
  consumePendingResponse,
  beforeMicOpen,
  failureLabel
}: RealtimeConversationOptions) {
  const [status, setStatus] = useState<ConversationStatus>('idle')
  const [muted, setMuted] = useState(false)
  const [level, setLevel] = useState(0)
  const clientRef = useRef<RealtimeVoiceClient | null>(null)
  const consultRef = useRef<TrackedConsult | null>(null)
  const generationRef = useRef(0)
  const busyRef = useRef(busy)
  busyRef.current = busy

  // Latest-ref for the callback props: the socket lifecycle must depend on
  // `enabled` only — inline callbacks changing identity per render must
  // never tear down and re-dial the session.
  const optionsRef = useRef({
    requestToken,
    submitTask,
    onInterrupt,
    onFatalError,
    onStopWord,
    isStopWord,
    pendingResponse,
    consumePendingResponse,
    beforeMicOpen,
    failureLabel
  })

  optionsRef.current = {
    requestToken,
    submitTask,
    onInterrupt,
    onFatalError,
    onStopWord,
    isStopWord,
    pendingResponse,
    consumePendingResponse,
    beforeMicOpen,
    failureLabel
  }

  const teardown = useCallback(() => {
    generationRef.current += 1
    consultRef.current = null
    clientRef.current?.close()
    clientRef.current = null
    setStatus('idle')
    setLevel(0)
  }, [])

  const completeConsult = useCallback(() => {
    const client = clientRef.current
    const consult = consultRef.current

    if (!client || !consult) {
      return
    }

    const reply = optionsRef.current.pendingResponse()

    if (!reply || reply.pending || busyRef.current) {
      return
    }

    // Attribute the reply before speaking it: a turn triggered by a different
    // submission (the user typed a message mid-consult) must not become the
    // consult's result. Mark it spoken and keep waiting for the consult
    // turn's own reply. Containment matches steered continuations; an
    // unknown trigger (userText null) is treated as the consult's.
    const userText = reply.userText?.trim()

    if (userText && userText !== consult.task && !userText.includes(consult.task)) {
      optionsRef.current.consumePendingResponse()

      return
    }

    consultRef.current = null
    optionsRef.current.consumePendingResponse()
    let output = reply.text.trim() || 'Hermes finished with no text output.'

    if (output.length > MAX_CONSULT_OUTPUT_CHARS) {
      output = `${output.slice(0, MAX_CONSULT_OUTPUT_CHARS)}\n[truncated — full text is on screen]`
    }

    client.sendFunctionOutput(consult.callId, output)
  }, [])

  const handleFunctionCall = useCallback((call: RealtimeFunctionCall) => {
    const client = clientRef.current

    if (!client) {
      return
    }

    const opts = optionsRef.current

    if (call.name === CONSULT_TOOL_NAME) {
      const task = String(call.args.task ?? '').trim()

      if (!task) {
        client.sendFunctionOutput(call.callId, 'No task provided.')

        return
      }

      // Self-heal: a consult whose turn died without ever producing a reply
      // would otherwise block every future consult as "still working".
      const tracked = consultRef.current

      if (
        tracked &&
        !busyRef.current &&
        Date.now() - tracked.at >= STALE_CONSULT_MIN_AGE_MS &&
        !optionsRef.current.pendingResponse()
      ) {
        consultRef.current = null
        client.sendFunctionOutput(tracked.callId, 'That task failed without producing a result.')
      }

      if (consultRef.current) {
        client.sendFunctionOutput(
          call.callId,
          'Hermes is still working on the previous task; its result will arrive shortly.'
        )

        return
      }

      consultRef.current = { callId: call.callId, task, at: Date.now() }

      if (!client.lastResponseHadAudio) {
        client.speakAcknowledgment()
      }

      void opts.submitTask(task)

      return
    }

    if (call.name === STEER_TOOL_NAME) {
      const instruction = String(call.args.instruction ?? '').trim()

      if (!instruction || !consultRef.current) {
        client.sendFunctionOutput(
          call.callId,
          instruction
            ? 'No Hermes task is running — use consult_hermes to start one.'
            : 'No steering instruction provided.'
        )

        return
      }

      // Retarget: only the steered continuation reports back.
      consultRef.current = { callId: consultRef.current.callId, task: instruction, at: Date.now() }
      void opts.submitTask(instruction)

      if (busyRef.current) {
        void opts.onInterrupt?.()
      }

      client.sendFunctionOutput(call.callId, 'Steering applied — Hermes is adjusting course.')

      return
    }

    client.sendFunctionOutput(call.callId, `Unknown tool: ${call.name}`)
  }, [])

  const start = useCallback(async () => {
    if (clientRef.current) {
      return
    }

    const generation = ++generationRef.current
    setStatus('transcribing') // connecting: reuses the pill's spinner state

    try {
      await optionsRef.current.beforeMicOpen?.()
      const grant = await optionsRef.current.requestToken()

      if (generation !== generationRef.current) {
        return
      }

      if (!grant) {
        // Realtime unavailable (e.g. older backend without the token RPC).
        // Not fatal: the surface flips to the classic loop on its own.
        teardown()

        return
      }

      const client = new RealtimeVoiceClient()
      clientRef.current = client
      await client.connect(grant, {
        onFunctionCall: handleFunctionCall,
        onLevel: value => setLevel(value),
        onUserTranscript: text => {
          if (optionsRef.current.isStopWord(text)) {
            optionsRef.current.onStopWord?.()
          }
        },
        onStatus: (clientStatus, detail) => {
          if (generation !== generationRef.current) {
            return
          }

          if (clientStatus === 'speaking') {
            setStatus('speaking')
          } else if (clientStatus === 'listening') {
            setStatus(consultRef.current || busyRef.current ? 'thinking' : 'listening')
          } else if (clientStatus === 'error') {
            notifyError(new Error(detail ?? 'realtime voice error'), optionsRef.current.failureLabel)
          } else if (clientStatus === 'closed' && clientRef.current) {
            // Server-side close (token expiry, network) — surface and stop.
            teardown()
            optionsRef.current.onFatalError?.()
          }
        }
      })

      if (generation !== generationRef.current) {
        client.close()

        return
      }

      setStatus('listening')
    } catch (error) {
      if (generation === generationRef.current) {
        teardown()
        notifyError(error, optionsRef.current.failureLabel)
        optionsRef.current.onFatalError?.()
      }
    }
  }, [handleFunctionCall, teardown])

  const end = useCallback(async () => {
    teardown()
  }, [teardown])

  // Consult completion watcher: the turn is done when busy settles and the
  // reply text finalized — the same signal the classic voice loop uses.
  useEffect(() => {
    if (!enabled) {
      return
    }

    const timer = setInterval(() => {
      if (consultRef.current) {
        completeConsult()
      }
    }, CONSULT_POLL_MS)

    return () => clearInterval(timer)
  }, [completeConsult, enabled])

  // Reflect thinking/listening as Hermes work starts and settles.
  useEffect(() => {
    if (!enabled || !clientRef.current) {
      return
    }

    if (busy || consultRef.current) {
      setStatus(prev => (prev === 'speaking' ? prev : 'thinking'))
    } else {
      setStatus(prev => (prev === 'thinking' ? 'listening' : prev))
    }
  }, [busy, enabled])

  // enabled drives the lifecycle, mirroring useVoiceConversation.
  useEffect(() => {
    if (enabled) {
      void start()
    } else {
      teardown()
    }
  }, [enabled, start, teardown])

  useEffect(() => () => teardown(), [teardown])

  const toggleMute = useCallback(() => {
    setMuted(prev => {
      const next = !prev
      clientRef.current?.setMuted(next)

      return next
    })
  }, [])

  // Realtime turns are endpointed by server VAD — no manual stop.
  const stopTurn = useCallback(() => undefined, [])

  return { end, level, muted, start, status, stopTurn, toggleMute }
}
