import { describe, expect, it, vi } from 'vitest'

import { createVoicePlayoutController, type VoiceAudioFrame, type VoiceAudioSink } from './voice-playout'

function frame(sequence: number, sampleOffset: number, sampleCount = 480): VoiceAudioFrame {
  return {
    sampleCount,
    sampleOffset,
    sequence,
    samples: new Int16Array(sampleCount)
  }
}

function fakeSink() {
  const sink: VoiceAudioSink & {
    writes: Float32Array[]
    started: boolean
    stopped: boolean
    resolveDrain: (() => void) | null
  } = {
    drain: vi.fn(
      () =>
        new Promise<void>(resolve => {
          sink.resolveDrain = resolve
        })
    ),
    pause: vi.fn(),
    onUnderrun: undefined,
    start: vi.fn(() => {
      sink.started = true
    }),
    stop: vi.fn(() => {
      sink.stopped = true
    }),
    writes: [],
    write: vi.fn((samples: Float32Array) => {
      sink.writes.push(samples)
    }),
    started: false,
    stopped: false,
    resolveDrain: null
  }

  return sink
}

describe('VoicePlayoutController', () => {
  it('waits for the adaptive startup target, then writes a continuous sequence', () => {
    const sink = fakeSink()
    const controller = createVoicePlayoutController(sink, {
      initialBufferMs: 40,
      maxBufferMs: 200,
      sampleRate: 24_000
    })

    controller.push(frame(0, 0, 480))
    expect(sink.start).not.toHaveBeenCalled()
    controller.push(frame(1, 480, 480))

    expect(sink.start).toHaveBeenCalledWith(24_000, 1)
    expect(sink.write).toHaveBeenCalledTimes(2)
    expect(controller.getState().started).toBe(true)
    expect(controller.getState().bufferedMs).toBe(0)

    controller.push(frame(2, 960, 480))
    expect(sink.write).toHaveBeenCalledTimes(3)
  })

  it('starts early when the stream ends and drains queued audio', async () => {
    const sink = fakeSink()
    const controller = createVoicePlayoutController(sink, {
      initialBufferMs: 200,
      maxBufferMs: 400,
      sampleRate: 24_000
    })

    controller.push(frame(0, 0, 480))
    const done = controller.end()

    expect(sink.start).toHaveBeenCalled()
    sink.resolveDrain?.()
    await done
    expect(sink.drain).toHaveBeenCalled()
  })

  it('raises the startup target after an underrun and cautiously decays it after stability', () => {
    const sink = fakeSink()
    const controller = createVoicePlayoutController(sink, {
      initialBufferMs: 100,
      maxBufferMs: 300,
      sampleRate: 24_000,
      stableFramesToDecrease: 2
    })

    expect(controller.getState().targetBufferMs).toBe(100)
    controller.reportUnderrun()
    expect(controller.getState().targetBufferMs).toBeGreaterThan(100)
    const raised = controller.getState().targetBufferMs
    controller.reportStablePlayback()
    controller.reportStablePlayback()
    expect(controller.getState().targetBufferMs).toBeLessThan(raised)
    expect(controller.getState().targetBufferMs).toBeGreaterThanOrEqual(100)
  })

  it('records sink underruns as telemetry and adapts the target', () => {
    const sink = fakeSink()
    const controller = createVoicePlayoutController(sink, { initialBufferMs: 40, maxBufferMs: 200 })

    sink.onUnderrun?.()

    expect(controller.getTelemetry().underruns).toBe(1)
    expect(controller.getState().targetBufferMs).toBeGreaterThan(40)
  })

  it('re-buffers after a post-start underrun before resuming the sink clock', () => {
    const sink = fakeSink()
    const controller = createVoicePlayoutController(sink, {
      initialBufferMs: 20,
      maxBufferMs: 200,
      sampleRate: 24_000
    })

    controller.push(frame(0, 0))
    sink.onUnderrun?.()
    expect(sink.pause).toHaveBeenCalledOnce()

    controller.push(frame(1, 480))
    expect(sink.write).toHaveBeenCalledTimes(1)
    controller.push(frame(2, 960))
    controller.push(frame(3, 1440))

    expect(sink.start).toHaveBeenCalledTimes(2)
    expect(sink.write).toHaveBeenCalledTimes(4)
  })

  it('bounds queued audio and reports overflow without accumulating latency', () => {
    const sink = fakeSink()
    const onError = vi.fn()
    const controller = createVoicePlayoutController(sink, {
      initialBufferMs: 1000,
      maxBufferMs: 10,
      sampleRate: 24_000,
      onError
    })

    controller.push(frame(0, 0, 480))
    expect(onError).toHaveBeenCalledWith('buffer-overflow')
    expect(controller.getTelemetry().queueOverflows).toBe(1)
    expect(controller.getState().bufferedMs).toBe(0)
  })

  it('rejects sequence and sample-offset violations', () => {
    const sink = fakeSink()
    const onError = vi.fn()
    const controller = createVoicePlayoutController(sink, { onError })

    controller.push(frame(0, 0))
    controller.push(frame(2, 960))

    expect(onError).toHaveBeenCalledWith('ordering-violation')
    expect(controller.getTelemetry().orderingViolations).toBe(1)
    expect(sink.stop).toHaveBeenCalled()
  })

  it('rejects a stream whose first frame is not the origin', () => {
    const sink = fakeSink()
    const onError = vi.fn()
    const controller = createVoicePlayoutController(sink, { onError })

    controller.push(frame(1, 480))

    expect(onError).toHaveBeenCalledWith('ordering-violation')
    expect(sink.write).not.toHaveBeenCalled()
  })

  it('does not start or claim audio for an empty stream', async () => {
    const sink = fakeSink()
    const controller = createVoicePlayoutController(sink)

    await controller.end()

    expect(sink.start).not.toHaveBeenCalled()
    expect(controller.getTelemetry().framesReceived).toBe(0)
  })

  it('cancels immediately and makes later frames no-ops', () => {
    const sink = fakeSink()
    const controller = createVoicePlayoutController(sink)

    controller.push(frame(0, 0))
    controller.cancel()
    controller.push(frame(1, 480))

    expect(sink.stop).toHaveBeenCalled()
    expect(controller.getState().cancelled).toBe(true)
    expect(sink.write).not.toHaveBeenCalled()
  })
})
