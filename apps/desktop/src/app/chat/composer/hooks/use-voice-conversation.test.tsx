import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setVoicePlaybackState } from '@/store/voice-playback'

const micMocks = vi.hoisted(() => {
  const start = vi.fn()
  const stop = vi.fn()
  const cancel = vi.fn()

  return { cancel, handle: { cancel, start, stop }, start, stop }
})

const playbackMocks = vi.hoisted(() => ({
  markVoicePlaybackInterrupted: vi.fn(),
  playSpeechText: vi.fn(),
  startSpeechStream: vi.fn(),
  stopVoicePlayback: vi.fn()
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: {
          configureSpeechToText: 'Configure speech to text.',
          couldNotStartSession: 'Could not start voice session.',
          microphoneFailed: 'Microphone failed.',
          playbackFailed: 'Playback failed.',
          transcriptionFailed: 'Transcription failed.',
          unavailable: 'Voice unavailable.'
        }
      }
    }
  })
}))

vi.mock('@/lib/voice-barge-in', () => ({
  monitorSpeechDuringPlayback: vi.fn(() => vi.fn())
}))

vi.mock('@/lib/voice-playback', () => playbackMocks)

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({ handle: micMocks.handle, level: 0, recording: false })
}))

import { useVoiceConversation } from './use-voice-conversation'

interface PendingResponse {
  id: string
  pending: boolean
  text: string
}

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(res => {
    resolve = res
  })

  return { promise, resolve }
}

interface ConversationProps {
  busy: boolean
  enabled: boolean
  response: PendingResponse | null
}

function renderConversation() {
  const consumePendingResponse = vi.fn()
  const submitted = deferred<void>()
  const onSubmit = vi.fn(() => submitted.promise)
  const onTranscribeAudio = vi.fn(async () => 'hello')
  const initialProps: ConversationProps = { busy: false, enabled: false, response: null }

  const hook = renderHook(
    ({ busy, enabled, response }: ConversationProps) =>
      useVoiceConversation({
        busy,
        consumePendingResponse,
        enabled,
        onSubmit,
        onTranscribeAudio,
        pendingResponse: () => response
      }),
    { initialProps }
  )

  return { consumePendingResponse, hook, onSubmit, onTranscribeAudio, resolveSubmit: submitted.resolve }
}

async function submitFirstVoiceTurn(
  hook: ReturnType<typeof renderConversation>['hook'],
  onSubmit: ReturnType<typeof renderConversation>['onSubmit'],
  resolveSubmit: ReturnType<typeof renderConversation>['resolveSubmit'],
  response: PendingResponse
) {
  hook.rerender({ busy: false, enabled: true, response: null })
  await waitFor(() => expect(hook.result.current.status).toBe('listening'))
  expect(micMocks.start).toHaveBeenCalledTimes(1)

  const startOptions = micMocks.start.mock.calls[0]?.[0] as { onSilence?: () => void }

  act(() => startOptions.onSilence?.())
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
  hook.rerender({ busy: true, enabled: true, response: null })
  await act(async () => resolveSubmit())
  await waitFor(() => expect(hook.result.current.status).toBe('thinking'))
  hook.rerender({ busy: true, enabled: true, response })
  await waitFor(() => expect(playbackMocks.startSpeechStream).toHaveBeenCalledTimes(1))
}

describe('useVoiceConversation relisten loop', () => {
  let sequence: number

  beforeEach(() => {
    sequence = 7
    setVoicePlaybackState({
      audioElement: null,
      messageId: null,
      sequence,
      source: null,
      status: 'idle'
    })

    vi.clearAllMocks()
    micMocks.start.mockResolvedValue(undefined)
    micMocks.stop.mockResolvedValue({
      audio: new Blob(['voice'], { type: 'audio/webm' }),
      durationMs: 500,
      heardSpeech: true
    })
    playbackMocks.stopVoicePlayback.mockImplementation(() => {
      sequence += 1
      setVoicePlaybackState({
        audioElement: null,
        messageId: null,
        sequence,
        source: null,
        status: 'idle'
      })
    })
  })

  afterEach(async () => {
    cleanup()
    vi.useRealTimers()
  })

  it('listens again after streaming speech finishes normally', async () => {
    const speech = deferred<'done' | 'fallback'>()

    const session = {
      append: vi.fn(),
      done: speech.promise,
      finish: vi.fn()
    }

    playbackMocks.startSpeechStream.mockImplementation(async () => {
      playbackMocks.stopVoicePlayback()

      return session
    })

    const response: PendingResponse = { id: 'assistant-1', pending: true, text: 'Hello there.' }
    const { hook, onSubmit, resolveSubmit } = renderConversation()

    await submitFirstVoiceTurn(hook, onSubmit, resolveSubmit, response)
    hook.rerender({ busy: false, enabled: true, response })

    await act(async () => speech.resolve('done'))
    await waitFor(() => expect(micMocks.start).toHaveBeenCalledTimes(2), { timeout: 500 })

    await act(async () => hook.result.current.end())
  })

  it('listens again after fallback speech finishes normally', async () => {
    const speech = deferred<boolean>()

    playbackMocks.startSpeechStream.mockResolvedValue(null)
    playbackMocks.playSpeechText.mockImplementation(() => {
      playbackMocks.stopVoicePlayback()

      return speech.promise
    })

    const response: PendingResponse = { id: 'assistant-1', pending: false, text: 'Hello there.' }
    const { hook, onSubmit, resolveSubmit } = renderConversation()

    await submitFirstVoiceTurn(hook, onSubmit, resolveSubmit, response)
    hook.rerender({ busy: false, enabled: true, response })
    await waitFor(() => expect(playbackMocks.playSpeechText).toHaveBeenCalledTimes(1))

    await act(async () => speech.resolve(true))
    await waitFor(() => expect(micMocks.start).toHaveBeenCalledTimes(2), { timeout: 500 })

    await act(async () => hook.result.current.end())
  })

  it('stays idle when the user explicitly stops streaming speech', async () => {
    const speech = deferred<'done' | 'fallback'>()

    const session = {
      append: vi.fn(),
      done: speech.promise,
      finish: vi.fn()
    }

    playbackMocks.startSpeechStream.mockImplementation(async () => {
      playbackMocks.stopVoicePlayback()

      return session
    })

    const response: PendingResponse = { id: 'assistant-1', pending: true, text: 'Hello there.' }
    const { hook, onSubmit, resolveSubmit } = renderConversation()

    await submitFirstVoiceTurn(hook, onSubmit, resolveSubmit, response)
    hook.rerender({ busy: false, enabled: true, response })

    playbackMocks.stopVoicePlayback()
    await act(async () => speech.resolve('done'))
    await waitFor(() => expect(hook.result.current.status).toBe('idle'))
    expect(micMocks.start).toHaveBeenCalledTimes(1)

    await act(async () => hook.result.current.end())
  })

  it('does not start fallback speech after Stop during the streaming handoff', async () => {
    const streamed = deferred<'done' | 'fallback'>()

    const session = {
      append: vi.fn(),
      done: streamed.promise,
      finish: vi.fn()
    }

    playbackMocks.startSpeechStream.mockImplementation(async () => {
      playbackMocks.stopVoicePlayback()

      return session
    })
    playbackMocks.playSpeechText.mockImplementation(() => {
      playbackMocks.stopVoicePlayback()

      return Promise.resolve(true)
    })

    const response: PendingResponse = { id: 'assistant-1', pending: true, text: 'Hello there.' }
    const { hook, onSubmit, resolveSubmit } = renderConversation()

    await submitFirstVoiceTurn(hook, onSubmit, resolveSubmit, response)
    hook.rerender({ busy: false, enabled: true, response })

    await act(async () => streamed.resolve('fallback'))
    playbackMocks.stopVoicePlayback()
    response.pending = false
    hook.rerender({ busy: false, enabled: true, response })

    await act(async () => {
      await new Promise(resolve => window.setTimeout(resolve, 350))
    })

    expect(playbackMocks.playSpeechText).not.toHaveBeenCalled()
    expect(micMocks.start).toHaveBeenCalledTimes(1)
    expect(hook.result.current.status).toBe('idle')

    await act(async () => hook.result.current.end())
  })

  it('stays idle when the user explicitly stops fallback speech', async () => {
    const speech = deferred<boolean>()

    playbackMocks.startSpeechStream.mockResolvedValue(null)
    playbackMocks.playSpeechText.mockImplementation(() => {
      playbackMocks.stopVoicePlayback()

      return speech.promise
    })

    const response: PendingResponse = { id: 'assistant-1', pending: false, text: 'Hello there.' }
    const { hook, onSubmit, resolveSubmit } = renderConversation()

    await submitFirstVoiceTurn(hook, onSubmit, resolveSubmit, response)
    hook.rerender({ busy: false, enabled: true, response })
    await waitFor(() => expect(playbackMocks.playSpeechText).toHaveBeenCalledTimes(1))

    playbackMocks.stopVoicePlayback()
    await act(async () => speech.resolve(true))
    await waitFor(() => expect(hook.result.current.status).toBe('idle'))
    expect(micMocks.start).toHaveBeenCalledTimes(1)

    await act(async () => hook.result.current.end())
  })

  it('does not reopen the microphone after voice mode ends during speech', async () => {
    const speech = deferred<'done' | 'fallback'>()

    const session = {
      append: vi.fn(),
      done: speech.promise,
      finish: vi.fn()
    }

    playbackMocks.startSpeechStream.mockImplementation(async () => {
      playbackMocks.stopVoicePlayback()

      return session
    })

    const response: PendingResponse = { id: 'assistant-1', pending: true, text: 'Hello there.' }
    const { hook, onSubmit, resolveSubmit } = renderConversation()

    await submitFirstVoiceTurn(hook, onSubmit, resolveSubmit, response)
    hook.rerender({ busy: false, enabled: true, response })
    hook.rerender({ busy: false, enabled: false, response })
    await waitFor(() => expect(hook.result.current.status).toBe('idle'))

    await act(async () => speech.resolve('done'))
    expect(micMocks.start).toHaveBeenCalledTimes(1)
  })
})
