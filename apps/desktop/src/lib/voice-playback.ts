import { resolveGatewayWsUrl } from '@hermes/shared'

import { getApiRequestProfile, speakText } from '@/hermes'
import {
  $voicePlayback,
  setVoicePlaybackState,
  type VoicePlaybackSource,
  type VoicePlaybackState
} from '@/store/voice-playback'

import { sanitizeTextForSpeech } from './speech-text'
import {
  createVoicePlayoutController,
  type VoiceAudioFrame,
  type VoiceAudioSink,
  type VoicePlayoutController,
  type VoicePlayoutTelemetry
} from './voice-playout'

// Free Edge TTS occasionally hands back audio that never fires `playing`/`ended`
// nor `error` — leaving voice mode stuck "speaking" forever. Reject if playback
// fails to start or stalls mid-stream for this long (rearmed on each progress
// tick, so legitimately long speech is never cut off).
const PLAYBACK_STALL_MS = 15_000

let currentAudio: HTMLAudioElement | null = null
let currentStop: (() => void) | null = null
let sequence = 0

// A shared, lazily-created AudioContext used only to nudge the browser's
// autoplay state out of "suspended". A wake-word-started voice turn has no
// preceding user gesture, so the first HTMLAudioElement.play() can be rejected
// with NotAllowedError. resume()-ing a context is the documented way to recover
// once the app is allowed to make sound; on Electron chat windows the
// no-user-gesture-required policy means this is already unlocked, so this is a
// cheap no-op fallback for other surfaces.
let unlockCtx: AudioContext | null = null

async function unlockAutoplay(): Promise<void> {
  if (typeof window === 'undefined') {
    return
  }

  const Ctor =
    window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext

  if (!Ctor) {
    return
  }

  if (!unlockCtx) {
    unlockCtx = new Ctor()
  }

  if (unlockCtx.state === 'suspended') {
    await unlockCtx.resume()
  }
}

function currentState(
  status: VoicePlaybackState['status'],
  options?: VoicePlaybackOptions,
  audioElement: HTMLAudioElement | null = null
): VoicePlaybackState {
  return {
    audioElement,
    messageId: options?.messageId ?? null,
    sequence,
    source: options?.source ?? null,
    status
  }
}

export interface VoicePlaybackOptions {
  messageId?: string | null
  source: VoicePlaybackSource
}

export function stopVoicePlayback() {
  sequence += 1
  currentStop?.()
  currentStop = null

  if (currentAudio) {
    currentAudio.pause()
    currentAudio.src = ''
    currentAudio.load()
    currentAudio = null
  }

  setVoicePlaybackState({
    audioElement: null,
    messageId: null,
    sequence,
    source: null,
    status: 'idle'
  })
}

// ---------------------------------------------------------------------------
// Streaming path — /api/audio/speak-stream WebSocket. Versioned streams feed
// one bounded AudioWorklet ring-buffer clock; legacy raw PCM remains on its
// compatibility scheduler. Speech starts while synthesis is still running.
// ---------------------------------------------------------------------------

async function resolveSpeakStreamUrl(): Promise<null | string> {
  const desktop = window.hermesDesktop

  if (!desktop?.getConnection) {
    return null
  }

  try {
    // Mint a fresh credential (single-use ticket in OAuth mode) for the
    // ACTIVE profile's backend, then swap the gateway endpoint for the PCM
    // one — auth is shared across WS routes.
    const profile = getApiRequestProfile()
    const wsUrl = await resolveGatewayWsUrl(desktop, await desktop.getConnection(profile))
    const url = new URL(wsUrl)

    if (!url.pathname.endsWith('/api/ws')) {
      return null
    }

    url.pathname = url.pathname.replace(/\/api\/ws$/, '/api/audio/speak-stream')

    // The backend resolves the TTS provider chain from this profile's
    // config/.env (same seam as /api/pty?profile=).
    if (profile) {
      url.searchParams.set('profile', profile)
    }

    return url.toString()
  } catch {
    return null
  }
}

export interface SpeechStreamSession {
  /** Feed more reply text as it streams in. Safe after `finish` (no-op). */
  append: (text: string) => void
  /** No more text coming — resolves `done` once the audio drains. */
  finish: () => void
  /**
   * 'done'    — audio fully played (or barged via stopVoicePlayback)
   * 'fallback'— no audio ever produced; caller should speak the accumulated
   *             text through `playSpeechText` instead.
   */
  done: Promise<'done' | 'fallback'>
  /** Snapshot of provider-neutral playout counters for diagnostics. */
  getTelemetry?: () => VoicePlayoutTelemetry
}

export type VoiceStreamProtocol = 'hermes.audio.v1' | 'legacy'

export function negotiateVoiceStreamProtocol(frame: {
  encoding?: string
  protocol?: string
  protocol_version?: string
  sample_rate?: number
  version?: number | string
}): VoiceStreamProtocol {
  const versioned =
    frame.protocol === 'hermes.audio.v1' ||
    frame.protocol_version === 'hermes.audio.v1' ||
    frame.version === 'hermes.audio.v1'

  return versioned ? 'hermes.audio.v1' : 'legacy'
}

async function createAudioWorkletSink(context: AudioContext, maxBufferSamples: number): Promise<VoiceAudioSink> {
  const moduleUrl = new URL('audio-worklet/voice-playout-processor.js', window.location.href)
  await context.audioWorklet.addModule(moduleUrl.toString())

  const node = new AudioWorkletNode(context, 'hermes-voice-playout', {
    numberOfInputs: 0,
    numberOfOutputs: 1,
    processorOptions: { maxBufferSamples }
  })
  node.connect(context.destination)
  let drainId = 0
  const pendingDrains = new Map<number, () => void>()
  let sink: VoiceAudioSink

  node.port.onmessage = event => {
    const message = event.data as { id?: number; type?: string }

    if (message.type === 'underrun') {
      sink.onUnderrun?.()
    } else if (message.type === 'overflow') {
      sink.onOverflow?.()
    } else if (message.type === 'stable') {
      sink.onStablePlayback?.()
    } else if (message.type === 'drained' && typeof message.id === 'number') {
      pendingDrains.get(message.id)?.()
      pendingDrains.delete(message.id)
    }
  }

  let stopped = false
  sink = {
    drain: () => {
      if (stopped) {
        return
      }

      const id = ++drainId

      return new Promise<void>(resolve => {
        pendingDrains.set(id, resolve)
        node.port.postMessage({ id, type: 'drain' })
      })
    },
    pause: () => {
      if (!stopped) {
        node.port.postMessage({ type: 'pause' })
      }
    },
    start: () => {
      if (!stopped) {
        node.port.postMessage({ type: 'start' })
      }
    },
    stop: () => {
      if (stopped) {
        return
      }

      stopped = true
      node.port.postMessage({ type: 'cancel' })
      node.disconnect()
      pendingDrains.forEach(resolve => resolve())
      pendingDrains.clear()
    },
    write: samples => {
      if (!stopped) {
        node.port.postMessage({ samples: samples.buffer, type: 'write' }, [samples.buffer])
      }
    }
  }

  return sink
}

/**
 * Open a live speech session: one WebSocket + one AudioContext for a whole
 * reply. Text is appended as LLM deltas arrive; the server cuts sentences and
 * streams PCM back while generation continues, so speech overlaps the text
 * stream (ChatGPT-style) with no per-sentence connection or synthesis gaps.
 */
function openSpeechStream(wsUrl: string, options: VoicePlaybackOptions): SpeechStreamSession {
  const ws = new WebSocket(wsUrl)
  ws.binaryType = 'arraybuffer'

  let context: AudioContext | null = null
  let streamRate = 24_000
  let nextStartAt = 0
  let carry: null | Uint8Array = null
  let versioned = false
  let controller: VoicePlayoutController | null = null
  let latestTelemetry: VoicePlayoutTelemetry = {
    framesReceived: 0,
    maxBufferedMs: 0,
    orderingViolations: 0,
    queueOverflows: 0,
    samplesReceived: 0,
    underruns: 0
  }
  let pendingAudioMeta: null | {
    sample_count?: number
    sample_offset?: number
    sampleCount?: number
    sampleOffset?: number
    sequence?: number
  } = null
  const pendingVersionedAudio: Array<{
    data: ArrayBuffer
    meta: {
      sample_count?: number
      sample_offset?: number
      sampleCount?: number
      sampleOffset?: number
      sequence?: number
    }
  }> = []
  let versionedEnd = false
  let maxPendingVersionedFrames = 50
  let versionedInit: Promise<void> | null = null
  let started = false
  let settled = false
  let finished = false
  const pendingSends: string[] = []

  let settle: (value: 'done' | 'fallback') => void = () => undefined

  const done = new Promise<'done' | 'fallback'>(resolve => {
    settle = value => {
      if (settled) {
        return
      }

      settled = true
      currentStop = null

      try {
        ws.close()
      } catch {
        // already closed
      }

      void context?.close().catch(() => undefined)
      context = null
      resolve(value)
    }
  })

  const send = (frame: object) => {
    const data = JSON.stringify(frame)

    if (ws.readyState === WebSocket.OPEN) {
      ws.send(data)
    } else if (ws.readyState === WebSocket.CONNECTING) {
      pendingSends.push(data)
    }
  }

  // stopVoicePlayback() → immediate barge-in: kill the socket (the server
  // aborts synthesis on disconnect) and the audio context (cuts sound now).
  currentStop = () => {
    controller?.cancel()

    if (ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({ type: 'stop' }))
      } catch {
        // Closing the socket below remains the cancellation fallback.
      }
    }

    settle('done')
  }

  const finishWhenDrained = () => {
    if (controller) {
      if (controller.getTelemetry().framesReceived === 0) {
        settle('fallback')
        return
      }

      void controller
        .end()
        .then(() => settle('done'))
        .catch(() => settle(started ? 'done' : 'fallback'))
      return
    }

    const remainingMs = context ? Math.max(0, nextStartAt - context.currentTime) * 1_000 : 0
    window.setTimeout(() => settle('done'), remainingMs + 100)
  }

  const scheduleLegacy = (data: ArrayBuffer) => {
    if (!context) {
      context = new AudioContext()
      if (context.state === 'suspended') {
        void context.resume().catch(() => undefined)
      }
    }

    if (!context) {
      return
    }

    // Provider chunks are not sample-aligned — carry any odd byte over.
    let bytes = new Uint8Array(data)

    if (carry) {
      const joined = new Uint8Array(carry.length + bytes.length)
      joined.set(carry)
      joined.set(bytes, carry.length)
      bytes = joined
      carry = null
    }

    const usable = bytes.length - (bytes.length % 2)

    if (bytes.length !== usable) {
      carry = bytes.slice(usable)
    }

    if (!usable) {
      return
    }

    const pcm = new Int16Array(bytes.buffer, bytes.byteOffset, usable / 2)
    const buffer = context.createBuffer(1, pcm.length, streamRate)
    const channel = buffer.getChannelData(0)

    for (let index = 0; index < pcm.length; index += 1) {
      channel[index] = pcm[index] / 32_768
    }

    const source = context.createBufferSource()
    source.buffer = buffer
    source.connect(context.destination)

    const startAt = Math.max(context.currentTime + 0.05, nextStartAt)
    source.start(startAt)
    nextStartAt = startAt + buffer.duration

    if (!started) {
      started = true
      setVoicePlaybackState(currentState('speaking', options))
    }
  }

  const pushVersioned = (
    data: ArrayBuffer,
    meta: {
      sample_count?: number
      sample_offset?: number
      sampleCount?: number
      sampleOffset?: number
      sequence?: number
    }
  ) => {
    if (!controller) {
      if (pendingVersionedAudio.length >= maxPendingVersionedFrames) {
        settle(started ? 'done' : 'fallback')
        return
      }
      pendingVersionedAudio.push({ data, meta })
      return
    }

    const bytes = new Uint8Array(data)

    if (bytes.byteLength % 2 !== 0) {
      controller.push({
        sampleCount: Number(meta.sample_count ?? meta.sampleCount ?? 0),
        sampleOffset: Number(meta.sample_offset ?? meta.sampleOffset ?? 0),
        sequence: Number(meta.sequence ?? 0),
        samples: new Int16Array()
      })
      return
    }

    const samples = new Int16Array(bytes.byteLength / 2)
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)

    for (let index = 0; index < samples.length; index += 1) {
      samples[index] = view.getInt16(index * 2, true)
    }

    const frame: VoiceAudioFrame = {
      sampleCount: Number(meta.sample_count ?? meta.sampleCount ?? samples.length),
      sampleOffset: Number(meta.sample_offset ?? meta.sampleOffset ?? 0),
      sequence: Number(meta.sequence ?? 0),
      samples
    }
    controller.push(frame)
    latestTelemetry = controller.getTelemetry()

    if (!started && controller.getState().started) {
      started = true
      setVoicePlaybackState(currentState('speaking', options))
    }
  }

  const initializeVersioned = (frame: {
    channels?: number
    encoding?: string
    initial_buffer_ms?: number
    max_buffer_ms?: number
    initialBufferMs?: number
    maxBufferMs?: number
    sample_rate?: number
    sampleRate?: number
  }) => {
    if (versionedInit) {
      return versionedInit
    }

    streamRate = frame.sample_rate ?? frame.sampleRate ?? 24_000
    const channels = frame.channels ?? 1
    const maxBufferMs = frame.max_buffer_ms ?? frame.maxBufferMs ?? 1_000
    maxPendingVersionedFrames = Math.max(1, Math.ceil(maxBufferMs / 20))

    if (
      frame.encoding !== 'pcm_s16le' ||
      !Number.isInteger(streamRate) ||
      streamRate < 8_000 ||
      streamRate > 96_000 ||
      channels !== 1
    ) {
      settle('fallback')
      versionedInit = Promise.resolve()
      return versionedInit
    }

    try {
      // AudioWorklet samples are consumed on the AudioContext clock. Bind that
      // clock to the negotiated PCM rate; otherwise 44.1 kHz providers play at
      // the host's common 48 kHz rate (fast, pitched up, and prone to underrun).
      context = new AudioContext({ sampleRate: streamRate })
      if (context.sampleRate !== streamRate) {
        throw new Error('AudioContext did not honor the negotiated sample rate')
      }
    } catch {
      settle('fallback')
      versionedInit = Promise.resolve()
      return versionedInit
    }
    if (context.state === 'suspended') {
      void context.resume().catch(() => undefined)
    }

    versionedInit = createAudioWorkletSink(context, Math.ceil((maxBufferMs / 1_000) * streamRate))
      .then(sink => {
        if (settled) {
          sink.stop()
          return
        }

        controller = createVoicePlayoutController(sink, {
          channels,
          initialBufferMs: frame.initial_buffer_ms ?? frame.initialBufferMs,
          maxBufferMs,
          onError: () => {
            latestTelemetry = controller?.getTelemetry() ?? latestTelemetry
            settle(started ? 'done' : 'fallback')
          },
          onState: state => {
            if (!started && state.started) {
              started = true
              setVoicePlaybackState(currentState('speaking', options))
            }
          },
          sampleRate: streamRate
        })

        pendingVersionedAudio.splice(0).forEach(item => pushVersioned(item.data, item.meta))

        if (versionedEnd) {
          finishWhenDrained()
        }
      })
      .catch(() => {
        settle('fallback')
      })

    return versionedInit
  }

  ws.onopen = () => {
    pendingSends.splice(0).forEach(data => ws.send(data))
  }

  ws.onmessage = event => {
    if (typeof event.data !== 'string') {
      if (versioned) {
        if (versionedEnd) {
          controller?.cancel()
          settle(started ? 'done' : 'fallback')
        } else if (pendingAudioMeta) {
          pushVersioned(event.data as ArrayBuffer, pendingAudioMeta)
          pendingAudioMeta = null
        } else {
          controller?.cancel()
          settle(started ? 'done' : 'fallback')
        }
      } else {
        // Older gateways send raw PCM without a versioned start frame.
        scheduleLegacy(event.data as ArrayBuffer)
      }

      return
    }

    let frame: {
      channels?: number
      encoding?: string
      initial_buffer_ms?: number
      max_buffer_ms?: number
      protocol?: string
      protocol_version?: string
      sample_count?: number
      sample_offset?: number
      sampleCount?: number
      sampleOffset?: number
      sample_rate?: number
      sampleRate?: number
      sequence?: number
      type?: string
      version?: number | string
    }

    try {
      frame = JSON.parse(event.data) as typeof frame
    } catch {
      return
    }

    if (frame.type === 'start') {
      versioned = negotiateVoiceStreamProtocol(frame) === 'hermes.audio.v1'

      if (versioned) {
        void initializeVersioned(frame)
      } else {
        // The old start frame only carries format information. Keep its raw
        // PCM scheduling path intact until the versioned contract is observed.
        streamRate = frame.sample_rate || 24_000
        scheduleLegacy(new ArrayBuffer(0))
      }
    } else if (versioned && frame.type === 'audio') {
      if (pendingAudioMeta || versionedEnd) {
        controller?.cancel()
        settle(started ? 'done' : 'fallback')
      } else {
        pendingAudioMeta = frame
      }
    } else if (frame.type === 'end') {
      if (versioned) {
        if (pendingAudioMeta) {
          controller?.cancel()
          settle(started ? 'done' : 'fallback')
          return
        }
        versionedEnd = true
        if (controller) {
          finishWhenDrained()
        }
      } else {
        finishWhenDrained()
      }
    } else if (frame.type === 'error') {
      latestTelemetry = controller?.getTelemetry() ?? latestTelemetry
      controller?.cancel()
      settle(started ? 'done' : 'fallback')
    } else if (frame.type === 'fallback') {
      settle(started ? 'done' : 'fallback')
    }
  }

  // A drop before any audio means the endpoint is unavailable (old backend,
  // auth, network) → fall back. After audio started, replaying the whole
  // message via POST would stutter — treat what played as the playback.
  ws.onerror = () => settle(started ? 'done' : 'fallback')
  ws.onclose = () => {
    if (versioned && pendingAudioMeta) {
      controller?.cancel()
      settle(started ? 'done' : 'fallback')
      return
    }
    if (versioned && versionedEnd && versionedInit) {
      void versionedInit.then(() => finishWhenDrained())
      return
    }

    started ? finishWhenDrained() : settle('fallback')
  }

  return {
    // Raw deltas — the server strips markdown/emoji per *sentence*, which is
    // the only safe granularity when constructs span delta boundaries.
    append: text => {
      if (text && !finished && !settled) {
        send({ text })
      }
    },
    finish: () => {
      if (!finished && !settled) {
        finished = true
        send({ done: true })
      }
    },
    done,
    getTelemetry: () => controller?.getTelemetry() ?? latestTelemetry
  }
}

/**
 * Live-speak an in-progress reply: open a session, then `append` deltas and
 * `finish` when generation completes. Resolves null when streaming is
 * unavailable (old backend / non-chunked provider) — the caller falls back to
 * whole-text `playSpeechText`.
 */
export async function startSpeechStream(options: VoicePlaybackOptions): Promise<null | SpeechStreamSession> {
  const wsUrl = await resolveSpeakStreamUrl()

  if (!wsUrl) {
    return null
  }

  stopVoicePlayback()
  setVoicePlaybackState(currentState('preparing', options))

  const session = openSpeechStream(wsUrl, options)

  void session.done.then(outcome => {
    if (outcome === 'done') {
      setVoicePlaybackState(currentState('idle'))
    }
  })

  return session
}

/** One-shot playback of complete text over the streaming WS. */
function playSpeechStream(wsUrl: string, text: string, options: VoicePlaybackOptions): Promise<'fallback' | 'played'> {
  const session = openSpeechStream(wsUrl, options)
  session.append(text)
  session.finish()

  return session.done.then(outcome => (outcome === 'done' ? 'played' : 'fallback'))
}

async function playSpeechDataUrl(
  speakableText: string,
  options: VoicePlaybackOptions,
  isCurrent: () => boolean
): Promise<boolean> {
  const response = await speakText(speakableText)

  if (!isCurrent()) {
    return false
  }

  const audio = new Audio(response.data_url)
  currentAudio = audio
  setVoicePlaybackState(currentState('speaking', options, audio))

  await new Promise<void>((resolve, reject) => {
    let stall: number | null = null

    const cleanup = () => {
      if (stall !== null) {
        window.clearTimeout(stall)
        stall = null
      }

      audio.removeEventListener('ended', onEnded)
      audio.removeEventListener('error', onError)
      audio.removeEventListener('timeupdate', armStall)
      currentStop = null
    }

    const armStall = () => {
      if (stall !== null) {
        window.clearTimeout(stall)
      }

      stall = window.setTimeout(() => {
        cleanup()
        reject(new Error('Playback stalled'))
      }, PLAYBACK_STALL_MS)
    }

    const onEnded = () => {
      cleanup()
      resolve()
    }

    const onError = () => {
      cleanup()
      reject(new Error('Playback failed'))
    }

    currentStop = () => {
      cleanup()
      resolve()
    }

    audio.addEventListener('ended', onEnded, { once: true })
    audio.addEventListener('error', onError, { once: true })
    audio.addEventListener('timeupdate', armStall)
    armStall()
    // A wake-word-started turn has no user gesture, so the autoplay policy can
    // reject the first play() with NotAllowedError. Electron chat windows set
    // autoplayPolicy: no-user-gesture-required to prevent this, but retry once
    // after resuming a shared AudioContext as a fallback for other surfaces
    // (dashboard-embedded) so the first reply isn't silently dropped.
    void audio.play().catch(async () => {
      try {
        await unlockAutoplay()
        await audio.play()
      } catch {
        onError()
      }
    })
  })

  if (!isCurrent()) {
    return false
  }

  currentAudio = null

  return true
}

export async function playSpeechText(text: string, options: VoicePlaybackOptions): Promise<boolean> {
  stopVoicePlayback()

  const speakableText = sanitizeTextForSpeech(text)

  if (!speakableText) {
    return false
  }

  const ownSequence = sequence
  const isCurrent = () => ownSequence === sequence

  setVoicePlaybackState(currentState('preparing', options))

  try {
    // Streaming first; the POST data-URL path is the fallback for backends
    // without the WS endpoint or providers without a chunked API.
    const streamUrl = await resolveSpeakStreamUrl()

    if (streamUrl && isCurrent()) {
      const outcome = await playSpeechStream(streamUrl, speakableText, options)

      if (outcome === 'played') {
        if (!isCurrent()) {
          return false
        }

        setVoicePlaybackState(currentState('idle'))

        return true
      }
    }

    if (!isCurrent()) {
      return false
    }

    const played = await playSpeechDataUrl(speakableText, options, isCurrent)

    if (played) {
      setVoicePlaybackState(currentState('idle'))
    }

    return played
  } catch (error) {
    if (isCurrent()) {
      currentStop = null
      currentAudio = null
      setVoicePlaybackState(currentState('idle'))
    }

    throw error
  }
}

export function isVoicePlaybackActive() {
  return $voicePlayback.get().status !== 'idle'
}

// ---------------------------------------------------------------------------
// Interruption latch — the next prompt.submit carries `interrupted: true` so
// the model knows its spoken reply was cut off (it can react: "rude!").
// Marked by the barge-in paths (VAD, typing over playback); TTL'd so a stale
// barge never annotates an unrelated message minutes later.
// ---------------------------------------------------------------------------

const INTERRUPT_TTL_MS = 120_000
let interruptedAt: null | number = null

export function markVoicePlaybackInterrupted() {
  interruptedAt = Date.now()
}

export function takeVoicePlaybackInterrupted(): boolean {
  const at = interruptedAt
  interruptedAt = null

  return at !== null && Date.now() - at < INTERRUPT_TTL_MS
}
