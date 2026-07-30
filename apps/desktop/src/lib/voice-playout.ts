/**
 * Provider-neutral PCM playout coordination.  This module deliberately has no
 * React or Web Audio dependencies so the stream invariants can be tested with
 * a deterministic sink.
 */

export interface VoiceAudioFrame {
  sequence: number
  sampleOffset: number
  sampleCount: number
  samples: Int16Array
}

export interface VoiceAudioSink {
  start: (sampleRate: number, channels: number) => void | Promise<void>
  pause: () => void
  write: (samples: Float32Array) => void
  drain: () => void | Promise<void>
  stop: () => void
  /** Called by a sink when its audio clock has no samples to render. */
  onUnderrun?: () => void
  /** Called by a sink when its own ring reaches the bounded limit. */
  onOverflow?: () => void
  /** Called periodically while the audio clock consumes without underrun. */
  onStablePlayback?: () => void
}

export interface VoicePlayoutTelemetry {
  framesReceived: number
  samplesReceived: number
  underruns: number
  orderingViolations: number
  queueOverflows: number
  maxBufferedMs: number
}

export interface VoicePlayoutState {
  started: boolean
  ended: boolean
  cancelled: boolean
  failed: boolean
  bufferedMs: number
  targetBufferMs: number
  maxBufferMs: number
}

export interface VoicePlayoutOptions {
  sampleRate?: number
  channels?: number
  initialBufferMs?: number
  maxBufferMs?: number
  stableFramesToDecrease?: number
  onError?: (reason: 'buffer-overflow' | 'ordering-violation') => void
  onState?: (state: VoicePlayoutState) => void
}

export interface VoicePlayoutController {
  push: (frame: VoiceAudioFrame) => void
  end: () => Promise<void>
  cancel: () => void
  reportUnderrun: () => void
  reportStablePlayback: () => void
  getState: () => VoicePlayoutState
  getTelemetry: () => VoicePlayoutTelemetry
}

const DEFAULT_INITIAL_BUFFER_MS = 120
const DEFAULT_MAX_BUFFER_MS = 1_000

function toFloat32(samples: Int16Array): Float32Array {
  const output = new Float32Array(samples.length)

  for (let index = 0; index < samples.length; index += 1) {
    output[index] = samples[index] / 32_768
  }

  return output
}

export function createVoicePlayoutController(
  sink: VoiceAudioSink,
  options: VoicePlayoutOptions = {}
): VoicePlayoutController {
  const sampleRate = options.sampleRate ?? 24_000
  const channels = options.channels ?? 1
  const initialBufferMs = Math.max(0, options.initialBufferMs ?? DEFAULT_INITIAL_BUFFER_MS)
  const maxBufferMs = Math.max(0, options.maxBufferMs ?? DEFAULT_MAX_BUFFER_MS)
  const stableFramesToDecrease = Math.max(1, options.stableFramesToDecrease ?? 12)
  const targetCeilingMs = Math.max(0, maxBufferMs - 20)

  let state: VoicePlayoutState = {
    bufferedMs: 0,
    cancelled: false,
    ended: false,
    failed: false,
    maxBufferMs,
    started: false,
    targetBufferMs: Math.min(initialBufferMs, targetCeilingMs)
  }
  let telemetry: VoicePlayoutTelemetry = {
    framesReceived: 0,
    maxBufferedMs: 0,
    orderingViolations: 0,
    queueOverflows: 0,
    samplesReceived: 0,
    underruns: 0
  }
  let expectedSequence: number | null = null
  let expectedSampleOffset: number | null = null
  let stableFrames = 0
  let rebuffering = false
  let drainPromise: Promise<void> | null = null
  const queue: VoiceAudioFrame[] = []

  const publish = () => options.onState?.({ ...state })
  const fail = (reason: 'buffer-overflow' | 'ordering-violation') => {
    if (state.failed || state.cancelled) {
      return
    }

    state = { ...state, failed: true }
    options.onError?.(reason)
    sink.stop()
    queue.length = 0
    state = { ...state, bufferedMs: 0 }
    publish()
  }
  const flush = () => {
    if (!state.started || state.failed || state.cancelled) {
      return
    }

    while (queue.length > 0) {
      const next = queue.shift()

      if (!next) {
        break
      }

      sink.write(toFloat32(next.samples))
      state = {
        ...state,
        bufferedMs: Math.max(0, state.bufferedMs - (next.sampleCount / sampleRate) * 1_000)
      }
    }

    publish()
  }
  const maybeStart = () => {
    if (state.started || state.failed || state.cancelled) {
      return
    }

    if (state.bufferedMs < state.targetBufferMs && !state.ended) {
      return
    }

    state = { ...state, started: true }
    void sink.start(sampleRate, channels)
    publish()
    flush()
  }

  const controller: VoicePlayoutController = {
    push: frame => {
      if (state.cancelled || state.failed || state.ended) {
        return
      }

      const validSampleCount = frame.sampleCount === frame.samples.length && frame.sampleCount > 0
      const ordered =
        expectedSequence === null
          ? frame.sequence === 0 && frame.sampleOffset === 0
          : frame.sequence === expectedSequence && frame.sampleOffset === expectedSampleOffset

      if (!validSampleCount || !ordered) {
        telemetry = { ...telemetry, orderingViolations: telemetry.orderingViolations + 1 }
        fail('ordering-violation')
        return
      }

      const frameMs = (frame.sampleCount / sampleRate) * 1_000

      if (state.bufferedMs + frameMs > state.maxBufferMs) {
        telemetry = { ...telemetry, queueOverflows: telemetry.queueOverflows + 1 }
        fail('buffer-overflow')
        return
      }

      expectedSequence = frame.sequence + 1
      expectedSampleOffset = frame.sampleOffset + frame.sampleCount
      telemetry = {
        ...telemetry,
        framesReceived: telemetry.framesReceived + 1,
        maxBufferedMs: Math.max(telemetry.maxBufferedMs, state.bufferedMs + frameMs),
        samplesReceived: telemetry.samplesReceived + frame.sampleCount
      }
      queue.push(frame)
      state = { ...state, bufferedMs: state.bufferedMs + frameMs }
      publish()
      maybeStart()
      if (rebuffering && state.bufferedMs >= state.targetBufferMs) {
        rebuffering = false
        void sink.start(sampleRate, channels)
      }
      if (!rebuffering) {
        flush()
      }
    },
    end: () => {
      if (drainPromise) {
        return drainPromise
      }

      state = { ...state, ended: true }
      if (rebuffering && queue.length > 0) {
        rebuffering = false
        void sink.start(sampleRate, channels)
        flush()
      }
      if (telemetry.framesReceived > 0) {
        maybeStart()
      }

      if (state.cancelled || state.failed || telemetry.framesReceived === 0) {
        drainPromise = Promise.resolve()
      } else {
        drainPromise = Promise.resolve(sink.drain()).then(() => undefined)
      }

      publish()

      return drainPromise
    },
    cancel: () => {
      if (state.cancelled) {
        return
      }

      state = { ...state, cancelled: true, bufferedMs: 0 }
      queue.length = 0
      sink.stop()
      publish()
    },
    getState: () => ({ ...state }),
    getTelemetry: () => ({ ...telemetry }),
    reportStablePlayback: () => {
      if (state.cancelled || state.failed) {
        return
      }

      stableFrames += 1

      if (stableFrames >= stableFramesToDecrease) {
        stableFrames = 0
        state = {
          ...state,
          targetBufferMs: Math.max(initialBufferMs, state.targetBufferMs - Math.max(5, state.targetBufferMs * 0.1))
        }
        publish()
      }
    },
    reportUnderrun: () => {
      if (state.cancelled || state.failed) {
        return
      }

      stableFrames = 0
      rebuffering = state.started
      if (rebuffering) {
        sink.pause()
      }
      state = {
        ...state,
        targetBufferMs: Math.min(targetCeilingMs, Math.max(state.targetBufferMs + 20, state.targetBufferMs * 1.5))
      }
      publish()
    }
  }

  sink.onUnderrun = () => {
    telemetry = { ...telemetry, underruns: telemetry.underruns + 1 }
    controller.reportUnderrun()
  }
  sink.onOverflow = () => {
    telemetry = { ...telemetry, queueOverflows: telemetry.queueOverflows + 1 }
    fail('buffer-overflow')
  }
  sink.onStablePlayback = () => controller.reportStablePlayback()

  return controller
}
