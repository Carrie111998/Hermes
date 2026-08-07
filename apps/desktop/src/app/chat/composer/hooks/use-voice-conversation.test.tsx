import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { BargeMonitorCallbacks } from '@/lib/voice-barge-in'

import type { AudioTranscriptionOptions } from '../types'

import type { MicRecorderOptions, MicRecording } from './use-mic-recorder'
import { useVoiceConversation } from './use-voice-conversation'

// The full-duplex contract: the barge monitor is live across the WHOLE agent
// turn — generation (thinking) and playback (speaking) — so speaking over the
// model interrupts it mid-generation instead of the mic being deaf until TTS
// starts (the Windows report: interruption "never works" because the deaf
// window covered generation, and playback bleed made the old monitor's
// trigger unreachable).

const monitorCalls: BargeMonitorCallbacks[] = []
const stopMonitor = vi.fn()

vi.mock('@/lib/voice-barge-in', () => ({
  monitorSpeechDuringPlayback: (callbacks: BargeMonitorCallbacks) => {
    monitorCalls.push(callbacks)

    return stopMonitor
  }
}))

const markVoicePlaybackInterrupted = vi.fn()
const stopVoicePlayback = vi.fn()

vi.mock('@/lib/voice-playback', () => ({
  markVoicePlaybackInterrupted: () => markVoicePlaybackInterrupted(),
  playSpeechText: vi.fn(async () => true),
  startSpeechStream: vi.fn(async () => null),
  stopVoicePlayback: () => stopVoicePlayback()
}))

vi.mock('@/lib/thinking-sound', () => ({
  startThinkingSound: vi.fn(),
  stopThinkingSound: vi.fn()
}))

const micHandle = {
  cancel: vi.fn(),
  start: vi.fn(async () => undefined),
  stop: vi.fn<() => Promise<MicRecording | null>>(async () => null)
}

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({ handle: micHandle, level: 0, recording: false })
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          configureSpeechToText: 'configure STT',
          couldNotStartSession: 'could not start',
          microphoneFailed: 'mic failed',
          playbackFailed: 'playback failed',
          transcriptionFailed: 'transcription failed',
          unavailable: 'unavailable'
        }
      }
    }
  })
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

interface HookProps {
  busy: boolean
}

function renderConversation(overrides: { onInterrupt?: () => void; transcript?: string } = {}) {
  const onInterrupt = overrides.onInterrupt ?? vi.fn()

  // Mirrors the real app: submitting a turn makes the agent busy.
  const onBusyChange: { current: (busy: boolean) => void } = { current: () => undefined }

  const onSubmit = vi.fn(async () => {
    onBusyChange.current(true)
  })

  const onStopWord = vi.fn()

  // First transcription is the turn that starts the conversation; subsequent
  // ones are barge captures (the overridable transcript).
  let transcriptions = 0

  const onTranscribeAudio = vi.fn(async () =>
    transcriptions++ === 0 ? 'kick off the task' : (overrides.transcript ?? 'and another thing')
  )

  const hook = renderHook(
    ({ busy }: HookProps) =>
      useVoiceConversation({
        busy,
        consumePendingResponse: vi.fn(),
        enabled: true,
        onInterrupt,
        onStopWord,
        onSubmit,
        onTranscribeAudio,
        pendingResponse: () => null
      }),
    { initialProps: { busy: false } }
  )

  onBusyChange.current = busy => hook.rerender({ busy })

  return { hook, onInterrupt, onStopWord, onSubmit, onTranscribeAudio }
}

/** Drive the hook into the generation phase (turn submitted, model working). */
async function enterThinking(hook: ReturnType<typeof renderConversation>['hook']) {
  await act(async () => {
    await hook.result.current.start()
  })
  await waitFor(() => expect(hook.result.current.status).toBe('listening'))

  micHandle.stop.mockResolvedValueOnce({
    audio: new Blob(['q'], { type: 'audio/webm' }),
    durationMs: 900,
    heardSpeech: true
  })

  await act(async () => {
    hook.result.current.stopTurn()
  })
  await waitFor(() => expect(hook.result.current.status).toBe('thinking'))
}

describe('useVoiceConversation full-duplex barge-in', () => {
  beforeEach(() => {
    monitorCalls.length = 0
    vi.clearAllMocks()
    micHandle.start.mockResolvedValue(undefined)
    micHandle.stop.mockResolvedValue(null)
  })

  afterEach(cleanup)

  it('arms the barge monitor during generation (before any reply audio exists)', async () => {
    const { hook } = renderConversation()

    await act(async () => {
      await hook.result.current.start()
    })
    await enterThinking(hook)

    await waitFor(() => expect(hook.result.current.status).toBe('thinking'))
    // busy=true + thinking → the full-duplex monitor must be live.
    await waitFor(() => expect(monitorCalls.length).toBeGreaterThan(0))
  })

  it('interrupts the in-flight turn when speech trips mid-generation', async () => {
    const { hook, onInterrupt } = renderConversation()

    await act(async () => {
      await hook.result.current.start()
    })
    await enterThinking(hook)
    await waitFor(() => expect(monitorCalls.length).toBeGreaterThan(0))

    act(() => {
      monitorCalls.at(-1)?.onSpeech()
    })

    expect(onInterrupt).toHaveBeenCalledTimes(1)
    expect(markVoicePlaybackInterrupted).toHaveBeenCalled()
    expect(stopVoicePlayback).toHaveBeenCalled()
  })

  it('submits the captured interruption once the interrupt settles (busy clears)', async () => {
    const { hook, onSubmit } = renderConversation({ transcript: 'no, do it differently' })

    await act(async () => {
      await hook.result.current.start()
    })
    await enterThinking(hook)
    await waitFor(() => expect(monitorCalls.length).toBeGreaterThan(0))

    const monitor = monitorCalls.at(-1)

    act(() => {
      monitor?.onSpeech()
    })

    // Interrupt lands → the turn ends → busy flips false.
    hook.rerender({ busy: false })

    await act(async () => {
      monitor?.onUtterance?.(new Blob(['x'], { type: 'audio/webm' }))
    })

    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith('no, do it differently'))
  })

  it('does not interrupt when speech trips during playback (turn already done)', async () => {
    const { hook, onInterrupt } = renderConversation()

    await act(async () => {
      await hook.result.current.start()
    })
    await enterThinking(hook)
    await waitFor(() => expect(monitorCalls.length).toBeGreaterThan(0))

    // Turn finished; playback phase.
    hook.rerender({ busy: false })

    act(() => {
      monitorCalls.at(-1)?.onSpeech()
    })

    expect(onInterrupt).not.toHaveBeenCalled()
    expect(stopVoicePlayback).toHaveBeenCalled()
  })

  it('a spoken stop command in the barge capture ends the conversation instead of submitting', async () => {
    const { hook, onStopWord, onSubmit } = renderConversation({ transcript: 'stop' })

    await act(async () => {
      await hook.result.current.start()
    })
    await enterThinking(hook)
    await waitFor(() => expect(monitorCalls.length).toBeGreaterThan(0))

    const monitor = monitorCalls.at(-1)

    act(() => {
      monitor?.onSpeech()
    })
    hook.rerender({ busy: false })

    await act(async () => {
      monitor?.onUtterance?.(new Blob(['s'], { type: 'audio/webm' }))
    })

    await waitFor(() => expect(onStopWord).toHaveBeenCalledTimes(1))
    // Only the kickoff turn was submitted — the "stop" capture never was.
    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect(onSubmit).not.toHaveBeenCalledWith('stop')
  })

  it('re-arms a single monitor per turn (idempotent ensure)', async () => {
    const { hook } = renderConversation()

    await act(async () => {
      await hook.result.current.start()
    })
    await enterThinking(hook)
    await waitFor(() => expect(monitorCalls.length).toBeGreaterThan(0))

    const armed = monitorCalls.length

    // Effect re-runs (busy toggles, status changes) must not open more mics.
    hook.rerender({ busy: true })
    hook.rerender({ busy: true })

    expect(monitorCalls.length).toBe(armed)
  })
})

// Live captions: while the user is still speaking, short slices of audio are
// transcribed on-device as a *preview*. They must never leave the machine, and
// never outrank the final transcription of the same utterance.
describe('useVoiceConversation live caption previews', () => {
  beforeEach(() => {
    monitorCalls.length = 0
    vi.clearAllMocks()
    micHandle.start.mockResolvedValue(undefined)
    micHandle.stop.mockResolvedValue(null)
  })

  afterEach(cleanup)

  type TranscribeFn = (audio: Blob, options?: AudioTranscriptionOptions) => Promise<string>

  function renderForPreview(onTranscribeAudio: TranscribeFn) {
    // Mirrors the real app: submitting a turn makes the agent busy, so the
    // hook stays in 'thinking' instead of immediately re-opening the mic.
    const onBusyChange: { current: (busy: boolean) => void } = { current: () => undefined }

    const hook = renderHook(
      ({ busy }: HookProps) =>
        useVoiceConversation({
          busy,
          consumePendingResponse: vi.fn(),
          enabled: true,
          onStopWord: vi.fn(),
          onSubmit: vi.fn(async () => {
            onBusyChange.current(true)
          }),
          onTranscribeAudio,
          pendingResponse: () => null
        }),
      { initialProps: { busy: false } }
    )

    onBusyChange.current = busy => hook.rerender({ busy })

    return hook
  }

  /** Options handed to the recorder on the most recent open. The shared mock is
   *  declared argument-less, so recover the real call signature here. */
  function lastStartOptions(): MicRecorderOptions | undefined {
    const calls = micHandle.start.mock.calls as unknown as Array<[MicRecorderOptions | undefined]>

    return calls.at(-1)?.[0]
  }

  async function startListening(hook: ReturnType<typeof renderForPreview>) {
    await act(async () => {
      await hook.result.current.start()
    })
    await waitFor(() => expect(hook.result.current.status).toBe('listening'))
  }

  it('opens the mic with partial-audio slicing wired to the preview pass', async () => {
    const hook = renderForPreview(vi.fn(async () => 'final'))
    await startListening(hook)

    const options = lastStartOptions()

    expect(typeof options?.onPartialAudio).toBe('function')

    const sliceMs = options?.partialAudioMs ?? 0
    expect(sliceMs).toBeGreaterThan(0)
    // Bounded: past this much audio, previewing stops paying for itself.
    expect(options?.partialAudioMaxMs).toBeGreaterThan(sliceMs)
  })

  it('requests previews on-device only and shows the result as the caption', async () => {
    const onTranscribeAudio = vi.fn(async () => 'partial caption')
    const hook = renderForPreview(onTranscribeAudio)
    await startListening(hook)

    await act(async () => {
      await lastStartOptions()?.onPartialAudio?.(new Blob(['p'], { type: 'audio/webm' }))
    })

    // localOnly keeps preview audio off any cloud STT provider; previewOnly
    // marks it droppable so the final pass can pre-empt it.
    expect(onTranscribeAudio).toHaveBeenCalledWith(expect.any(Blob), { localOnly: true, previewOnly: true })
    await waitFor(() => expect(hook.result.current.transcript).toBe('partial caption'))
  })

  it('drops a preview that resolves after its turn already ended', async () => {
    let releasePreview: (value: string) => void = () => undefined

    const onTranscribeAudio = vi.fn(
      (_audio: Blob, options?: { previewOnly?: boolean }) =>
        options?.previewOnly
          ? new Promise<string>(resolve => {
              releasePreview = resolve
            })
          : Promise.resolve('final transcript')
    )

    const hook = renderForPreview(onTranscribeAudio)
    await startListening(hook)

    const partial = lastStartOptions()?.onPartialAudio?.(new Blob(['p'], { type: 'audio/webm' }))

    // The user stops talking and the turn closes while the preview is in flight.
    await act(async () => {
      await hook.result.current.end()
    })

    await act(async () => {
      releasePreview('stale caption')
      await partial
    })

    expect(hook.result.current.transcript).toBe('')
  })

  it('replaces the caption with the authoritative final transcription', async () => {
    const onTranscribeAudio = vi.fn(async (_audio: Blob, options?: { previewOnly?: boolean }) =>
      options?.previewOnly ? 'rough guess' : 'the real transcript'
    )

    const hook = renderForPreview(onTranscribeAudio)
    await startListening(hook)

    await act(async () => {
      await lastStartOptions()?.onPartialAudio?.(new Blob(['p'], { type: 'audio/webm' }))
    })
    await waitFor(() => expect(hook.result.current.transcript).toBe('rough guess'))

    micHandle.stop.mockResolvedValueOnce({
      audio: new Blob(['q'], { type: 'audio/webm' }),
      durationMs: 900,
      heardSpeech: true
    })

    await act(async () => {
      hook.result.current.stopTurn()
    })

    await waitFor(() => expect(hook.result.current.transcript).toBe('the real transcript'))
  })
})
