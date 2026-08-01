/**
 * P1 Voice Conversation Loop for the pet overlay window.
 *
 * The overlay is a gateway-less transparent BrowserWindow, but it DOES have
 * access to `window.hermesDesktop.api()` (the main-process HTTP proxy) and
 * `window.hermesDesktop.requestMicrophoneAccess()`. So it can:
 *
 *  1. Record audio from the microphone (browser getUserMedia + MediaRecorder).
 *  2. Send it to the backend for transcription (`/api/audio/transcribe`).
 *  3. Submit the transcript as a prompt via `petOverlay.control({type:'submit'})`.
 *  4. Speak the assistant's reply aloud via `/api/audio/speak` (data-URL TTS).
 *
 * This is a MANUAL / push-to-talk loop — the mic opens when the user clicks
 * "Listen" and closes on silence (or a second click). There is NO always-on
 * microphone. A visible recording indicator is shown while listening.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

// ── Types ────────────────────────────────────────────────────────────────────

export type OverlayVoiceStatus = 'idle' | 'listening' | 'transcribing' | 'thinking' | 'speaking'

export interface OverlayVoiceState {
  status: OverlayVoiceStatus
  /** Audio level 0–1 for the recording indicator bars. */
  level: number
  /** Elapsed seconds in the current listening turn. */
  elapsedSeconds: number
  /** The last transcript (for display). */
  lastTranscript: string | null
  /** Whether STT (transcription) is available on this backend. */
  sttAvailable: boolean | null
  /** Whether TTS (speech synthesis) is available on this backend. */
  ttsAvailable: boolean | null
  /** Whether voice replies are enabled (persisted pref). */
  voiceReplies: boolean
  /** Error message for display (auto-clears). */
  error: string | null
}

const initialState: OverlayVoiceState = {
  elapsedSeconds: 0,
  error: null,
  lastTranscript: null,
  level: 0,
  status: 'idle',
  sttAvailable: null,
  ttsAvailable: null,
  voiceReplies: false
}

// ── Mic recorder (self-contained, no external deps) ──────────────────────────

interface MicRecording {
  audio: Blob
  durationMs: number
  heardSpeech: boolean
}

const SILENCE_LEVEL = 0.075
const SILENCE_MS = 1_250
const IDLE_SILENCE_MS = 12_000
const MAX_RECORDING_MS = 60_000

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onloadend = () => resolve(reader.result as string)
    reader.onerror = () => reject(new Error('Failed to read audio'))
    reader.readAsDataURL(blob)
  })
}

// ── Gateway API helpers ──────────────────────────────────────────────────────

interface TranscribeResponse {
  transcript?: string
  text?: string
}

interface SpeakResponse {
  data_url?: string
  audio?: string
}

async function apiTranscribe(dataUrl: string, mimeType?: string): Promise<string> {
  const result = await window.hermesDesktop.api<TranscribeResponse>({
    path: '/api/audio/transcribe',
    method: 'POST',
    body: { data_url: dataUrl, mime_type: mimeType },
    timeoutMs: 180_000
  })

  return result.transcript ?? result.text ?? ''
}

async function apiSpeak(text: string): Promise<string | null> {
  const result = await window.hermesDesktop.api<SpeakResponse>({
    path: '/api/audio/speak',
    method: 'POST',
    body: { text },
    timeoutMs: 120_000
  })

  return result.data_url ?? result.audio ?? null
}

// ── Sanitize text for speech (strip markdown/emoji/code) ─────────────────────

function sanitizeForSpeech(text: string): string {
  return text
    .replace(/```[\s\S]*?(?:```|$)/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]*\n{2,}[ \t]*/g, '. ')
    .replace(/[ \t]*\n[ \t]*/g, ' ')
    .replace(/\bhttps?:\/\/\S+/gi, ' link ')
    .replace(/(?:[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}]|[\u{FE0F}\u{200D}]|[\u{E0020}-\u{E007F}])+/gu, ' ')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/[*_~>#]/g, '')
    .replace(/^\s*[-+*]\s+/gm, '')
    .replace(/\s+/g, ' ')
    .trim()
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export interface OverlayVoiceLoopOptions {
  /** Called when a transcript is ready to submit as a prompt. */
  onSubmit: (text: string) => void
  /** Called when voice replies toggle changes. */
  onVoiceRepliesChange?: (enabled: boolean) => void
  /** Whether the session is busy (agent is working). */
  busy: boolean
  /** The latest assistant reply text (for TTS). */
  lastReply: string | null
  /** A monotonic id that bumps when a new reply arrives. */
  replyId: number
  /** Initial voice replies preference. */
  initialVoiceReplies: boolean
}

export function useOverlayVoiceLoop({
  onSubmit,
  onVoiceRepliesChange,
  busy,
  lastReply,
  replyId,
  initialVoiceReplies
}: OverlayVoiceLoopOptions) {
  const [state, setState] = useState<OverlayVoiceState>({
    ...initialState,
    voiceReplies: initialVoiceReplies
  })

  // Refs for async-safe state access.
  const statusRef = useRef<OverlayVoiceStatus>('idle')
  const voiceRepliesRef = useRef(initialVoiceReplies)
  const busyRef = useRef(busy)
  const lastReplyRef = useRef(lastReply)
  const replyIdRef = useRef(replyId)
  const spokenReplyIdRef = useRef(replyId)
  const disposedRef = useRef(false)

  // Mic recording refs.
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const audioContextRef = useRef<AudioContext | null>(null)
  const animationRef = useRef<number | null>(null)
  const startedAtRef = useRef(0)
  const heardSpeechRef = useRef(false)
  const silenceTriggeredRef = useRef(false)
  const silenceStartedAtRef = useRef<number | null>(null)
  const stopResolverRef = useRef<((recording: MicRecording | null) => void) | null>(null)
  const maxTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const elapsedIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const currentAudioRef = useRef<HTMLAudioElement | null>(null)
  const errorTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const setStatus = useCallback((status: OverlayVoiceStatus) => {
    statusRef.current = status
    setState(prev => ({ ...prev, status }))
  }, [])

  const setError = useCallback((message: string) => {
    if (errorTimeoutRef.current) {
      clearTimeout(errorTimeoutRef.current)
    }

    setState(prev => ({ ...prev, error: message }))
    errorTimeoutRef.current = setTimeout(() => {
      setState(prev => ({ ...prev, error: null }))
    }, 4_000)
  }, [])

  const setLevel = useCallback((level: number) => {
    setState(prev => ({ ...prev, level }))
  }, [])

  const cleanupMic = useCallback(() => {
    if (animationRef.current) {
      cancelAnimationFrame(animationRef.current)
      animationRef.current = null
    }

    if (audioContextRef.current) {
      void audioContextRef.current.close().catch(() => undefined)
      audioContextRef.current = null
    }

    streamRef.current?.getTracks().forEach(track => track.stop())
    streamRef.current = null
    recorderRef.current = null
    setLevel(0)
    silenceTriggeredRef.current = false
  }, [setLevel])

  const stopElapsedTimer = useCallback(() => {
    if (elapsedIntervalRef.current) {
      clearInterval(elapsedIntervalRef.current)
      elapsedIntervalRef.current = null
    }

    if (maxTimeoutRef.current) {
      clearTimeout(maxTimeoutRef.current)
      maxTimeoutRef.current = null
    }
  }, [])

  // ── Transcribe + submit ──────────────────────────────────────────────────

  const transcribeAndSubmit = useCallback(
    async (audio: Blob) => {
      setStatus('transcribing')
      setState(prev => ({ ...prev, elapsedSeconds: 0 }))

      try {
        const dataUrl = await blobToDataUrl(audio)
        const transcript = (await apiTranscribe(dataUrl, audio.type)).trim()

        setState(prev => ({ ...prev, lastTranscript: transcript || null, sttAvailable: true }))

        if (!transcript) {
          setStatus('idle')

          return
        }

        // Submit the transcript as a prompt.
        onSubmit(transcript)
        setStatus('thinking')
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'Transcription failed'

        // Check if it's a "not configured" error.
        if (msg.includes('not configured') || msg.includes('disabled') || msg.includes('unavailable')) {
          setState(prev => ({ ...prev, sttAvailable: false }))
        }

        setError(msg)
        setStatus('idle')
      }
    },
    [onSubmit, setError, setStatus]
  )

  // ── Mic recording lifecycle ──────────────────────────────────────────────

  const stopRecording = useCallback((): Promise<MicRecording | null> => {
    return new Promise(resolve => {
      const recorder = recorderRef.current

      if (!recorder || recorder.state === 'inactive') {
        cleanupMic()
        stopElapsedTimer()
        resolve(null)

        return
      }

      stopResolverRef.current = resolve
      recorder.stop()
    })
  }, [cleanupMic, stopElapsedTimer])

  const handleTurnEnd = useCallback(async () => {
    if (statusRef.current !== 'listening') {
      return
    }

    stopElapsedTimer()
    setStatus('transcribing')

    const result = await stopRecording()

    if (!result || (!result.heardSpeech)) {
      setStatus('idle')

      return
    }

    await transcribeAndSubmit(result.audio)
  }, [setStatus, stopElapsedTimer, stopRecording, transcribeAndSubmit])

  const startMeter = useCallback(
    (stream: MediaStream) => {
      const AudioContextCtor =
        window.AudioContext || (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext

      if (!AudioContextCtor) {
        return
      }

      try {
        const ctx = new AudioContextCtor()
        const analyser = ctx.createAnalyser()
        const source = ctx.createMediaStreamSource(stream)
        analyser.fftSize = 256
        const data = new Uint8Array(analyser.fftSize)
        source.connect(analyser)
        audioContextRef.current = ctx

        const tick = () => {
          if (disposedRef.current || statusRef.current !== 'listening') {
            return
          }

          analyser.getByteTimeDomainData(data)
          let sum = 0

          for (const value of data) {
            const centered = value - 128
            sum += centered * centered
          }

          const rms = Math.sqrt(sum / data.length)
          const normalized = Math.min(1, rms / 42)
          const now = Date.now()

          setLevel(normalized)

          if (normalized >= SILENCE_LEVEL) {
            heardSpeechRef.current = true
            silenceStartedAtRef.current = null
          } else if (heardSpeechRef.current && silenceStartedAtRef.current === null) {
            silenceStartedAtRef.current = now
          }

          // Auto-end on silence after speech.
          if (
            heardSpeechRef.current &&
            silenceStartedAtRef.current &&
            now - silenceStartedAtRef.current >= SILENCE_MS
          ) {
            silenceTriggeredRef.current = true
            void handleTurnEnd()

            return
          }

          // Auto-end on idle (no speech detected for a long time).
          if (!heardSpeechRef.current && now - startedAtRef.current >= IDLE_SILENCE_MS) {
            silenceTriggeredRef.current = true
            void handleTurnEnd()

            return
          }

          animationRef.current = requestAnimationFrame(tick)
        }

        tick()
      } catch {
        // Metering failed — recording still works, just no level display.
      }
    },
    [handleTurnEnd, setLevel]
  )

  const startListening = useCallback(async () => {
    if (statusRef.current !== 'idle') {
      return
    }

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setError('Microphone not supported in this environment')

      return
    }

    // Request microphone permission via IPC.
    const permitted = await window.hermesDesktop?.requestMicrophoneAccess?.()

    if (permitted === false) {
      setError('Microphone permission denied')

      return
    }

    let stream: MediaStream

    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true }
      })
    } catch (err) {
      const name = err instanceof DOMException ? err.name : ''

      if (name === 'NotAllowedError' || name === 'SecurityError') {
        setError('Microphone permission denied')
      } else if (name === 'NotFoundError') {
        setError('No microphone found')
      } else if (name === 'NotReadableError') {
        setError('Microphone in use by another app')
      } else {
        setError('Could not access microphone')
      }

      return
    }

    const mimeType =
      [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/mp4',
        'audio/ogg;codecs=opus'
      ].find(type => MediaRecorder.isTypeSupported(type)) ?? ''

    let recorder: MediaRecorder

    try {
      recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
    } catch {
      stream.getTracks().forEach(t => t.stop())
      setError('Could not initialize recorder')

      return
    }

    chunksRef.current = []
    streamRef.current = stream
    recorderRef.current = recorder
    heardSpeechRef.current = false
    silenceTriggeredRef.current = false
    silenceStartedAtRef.current = null
    startedAtRef.current = Date.now()

    recorder.ondataavailable = event => {
      if (event.data.size > 0) {
        chunksRef.current.push(event.data)
      }
    }

    recorder.onstop = () => {
      const chunks = chunksRef.current
      const type = recorder.mimeType || mimeType || 'audio/webm'
      const durationMs = Date.now() - startedAtRef.current
      const heardSpeech = heardSpeechRef.current
      chunksRef.current = []
      cleanupMic()

      const resolver = stopResolverRef.current
      stopResolverRef.current = null

      if (!chunks.length) {
        resolver?.(null)

        return
      }

      resolver?.({ audio: new Blob(chunks, { type }), durationMs, heardSpeech })
    }

    recorder.start()
    setStatus('listening')
    startMeter(stream)

    // Elapsed timer for display.
    elapsedIntervalRef.current = setInterval(() => {
      setState(prev => ({ ...prev, elapsedSeconds: (Date.now() - startedAtRef.current) / 1000 }))
    }, 250)

    // Max recording timeout.
    maxTimeoutRef.current = setTimeout(() => void handleTurnEnd(), MAX_RECORDING_MS)
  }, [cleanupMic, setError, setStatus, startMeter, handleTurnEnd])

  // ── Stop / cancel listening ──────────────────────────────────────────────

  const stopListening = useCallback(async () => {
    if (statusRef.current !== 'listening') {
      return
    }

    stopElapsedTimer()
    setStatus('transcribing')
    const result = await stopRecording()

    if (result) {
      await transcribeAndSubmit(result.audio)
    } else {
      setStatus('idle')
    }
  }, [setStatus, stopElapsedTimer, stopRecording, transcribeAndSubmit])

  const cancelListening = useCallback(() => {
    stopElapsedTimer()

    if (recorderRef.current && recorderRef.current.state !== 'inactive') {
      recorderRef.current.ondataavailable = null
      recorderRef.current.onstop = null
      recorderRef.current.stop()
    }

    cleanupMic()
    setStatus('idle')
  }, [cleanupMic, setStatus, stopElapsedTimer])

  // ── TTS playback ─────────────────────────────────────────────────────────

  const stopSpeaking = useCallback(() => {
    if (currentAudioRef.current) {
      currentAudioRef.current.pause()
      currentAudioRef.current.src = ''
      currentAudioRef.current = null
    }

    if (statusRef.current === 'speaking') {
      setStatus('idle')
    }
  }, [setStatus])

  const speakReply = useCallback(
    async (text: string) => {
      const speakable = sanitizeForSpeech(text)

      if (!speakable) {
        return
      }

      stopSpeaking()
      setStatus('speaking')

      try {
        const dataUrl = await apiSpeak(speakable)
        setState(prev => ({ ...prev, ttsAvailable: true }))

        if (!dataUrl || disposedRef.current) {
          setStatus('idle')

          return
        }

        const audio = new Audio(dataUrl)
        currentAudioRef.current = audio

        await new Promise<void>((resolve) => {
          let done = false

          const finish = () => {
            if (done) {
              return
            }

            done = true
            audio.removeEventListener('ended', onEnded)
            audio.removeEventListener('error', onError)
            resolve()
          }

          const onEnded = () => finish()
          const onError = () => finish()
          audio.addEventListener('ended', onEnded, { once: true })
          audio.addEventListener('error', onError, { once: true })
          void audio.play().catch(() => finish())
        })

        currentAudioRef.current = null

        if (statusRef.current === 'speaking') {
          setStatus('idle')
        }
      } catch (err) {
        const msg = err instanceof Error ? err.message : 'TTS failed'

        if (msg.includes('not configured') || msg.includes('disabled') || msg.includes('unavailable')) {
          setState(prev => ({ ...prev, ttsAvailable: false }))
        }

        setError(msg)
        setStatus('idle')
      }
    },
    [setError, setStatus, stopSpeaking]
  )

  // ── Voice replies toggle ─────────────────────────────────────────────────

  const toggleVoiceReplies = useCallback(() => {
    const next = !voiceRepliesRef.current
    voiceRepliesRef.current = next
    setState(prev => ({ ...prev, voiceReplies: next }))
    onVoiceRepliesChange?.(next)

    if (!next) {
      stopSpeaking()
    }
  }, [onVoiceRepliesChange, stopSpeaking])

  // ── Auto-speak new replies when voice replies is ON ──────────────────────

  useEffect(() => {
    if (
      voiceRepliesRef.current &&
      replyId > spokenReplyIdRef.current &&
      lastReply &&
      !busy &&
      statusRef.current !== 'listening' &&
      statusRef.current !== 'transcribing'
    ) {
      spokenReplyIdRef.current = replyId
      void speakReply(lastReply)
    }
  }, [replyId, lastReply, busy, speakReply])

  // ── Sync busy ref ────────────────────────────────────────────────────────

  useEffect(() => {
    busyRef.current = busy

    // When the agent finishes (busy goes false) and we were in 'thinking',
    // transition to idle. The auto-speak effect will pick up the reply.
    if (!busy && statusRef.current === 'thinking') {
      setStatus('idle')
    }
  }, [busy, setStatus])

  // ── Sync refs ────────────────────────────────────────────────────────────

  useEffect(() => {
    lastReplyRef.current = lastReply
  }, [lastReply])

  useEffect(() => {
    replyIdRef.current = replyId
  }, [replyId])

  // ── Cleanup on unmount ───────────────────────────────────────────────────

  useEffect(() => {
    disposedRef.current = false

    return () => {
      disposedRef.current = true
      cancelListening()
      stopSpeaking()
      stopElapsedTimer()

      if (errorTimeoutRef.current) {
        clearTimeout(errorTimeoutRef.current)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return {
    state,
    startListening,
    stopListening,
    cancelListening,
    stopSpeaking,
    toggleVoiceReplies
  }
}
