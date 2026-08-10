import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { MicRecorderOptions } from './use-mic-recorder'
import { useVoiceConversation } from './use-voice-conversation'

// The regression under test: after TTS finishes speaking a completed reply,
// the loop effect's completion branch arms pendingStartRef and used to early-
// return, counting on its setStatus('idle') to re-run the effect — but
// speak()'s finally had already left the status at 'idle', so React bailed,
// no re-render came, and the armed flag was never consumed. The mic stayed
// dead after the first spoken exchange.

const micStart = vi.fn<(options?: MicRecorderOptions) => Promise<void>>()
const micStop = vi.fn()
const micCancel = vi.fn()
let micOptions: MicRecorderOptions | undefined

vi.mock('./use-mic-recorder', () => ({
  useMicRecorder: () => ({
    handle: {
      start: (options?: MicRecorderOptions) => {
        micOptions = options

        return micStart(options)
      },
      stop: micStop,
      cancel: micCancel
    },
    level: 0,
    recording: false
  })
}))

vi.mock('@/lib/voice-playback', () => ({
  playSpeechText: vi.fn(() => Promise.resolve()),
  stopVoicePlayback: vi.fn()
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      notifications: {
        voice: new Proxy({} as Record<string, string>, { get: (_target, key) => String(key) })
      }
    }
  })
}))

interface HarnessProps {
  busy: boolean
  enabled: boolean
  response: { id: string; pending: boolean; text: string } | null
}

function renderConversation() {
  let props: HarnessProps = { busy: false, enabled: false, response: null }

  // Mirrors the real composer: submitting a voice turn makes the run busy
  // before handleTurn's own setStatus('thinking') lands — which is what keeps
  // the loop effect from reading a finished run out of a fresh turn.
  const onSubmit = vi.fn(() => update({ busy: true }))
  const onTranscribeAudio = vi.fn(async () => 'hello there')

  const view = renderHook(
    // Fresh pendingResponse/consumePendingResponse closures per render, the
    // way useComposerVoice really passes them — the effect must not depend
    // on stable identities to consume its own armed flag.
    (current: HarnessProps) =>
      useVoiceConversation({
        busy: current.busy,
        enabled: current.enabled,
        onSubmit,
        onTranscribeAudio,
        pendingResponse: () => current.response,
        consumePendingResponse: () => {}
      }),
    { initialProps: props }
  )

  function update(patch: Partial<HarnessProps>) {
    props = { ...props, ...patch }
    view.rerender(props)
  }

  return { onSubmit, onTranscribeAudio, update, view }
}

const settle = () => act(async () => {})

beforeEach(() => {
  micOptions = undefined
  micStart.mockReset().mockResolvedValue(undefined)
  micStop.mockReset()
  micCancel.mockReset()
})

describe('useVoiceConversation', () => {
  it('re-arms the mic after the spoken reply completes', async () => {
    const { onSubmit, update, view } = renderConversation()

    // Enable → the conversation opens the mic.
    await act(async () => update({ enabled: true }))
    await settle()
    expect(micStart).toHaveBeenCalledTimes(1)
    expect(view.result.current.status).toBe('listening')

    // The user speaks; VAD silence closes the turn and the transcript submits.
    micStop.mockResolvedValueOnce({ audio: new Blob(['x']), durationMs: 1200, heardSpeech: true })
    await act(async () => micOptions?.onSilence?.())
    expect(onSubmit).toHaveBeenCalledWith('hello there')
    expect(view.result.current.status).toBe('thinking')

    // The reply streams while the run is busy, and TTS speaks the first chunk.
    await act(async () => update({ response: { id: 'm1', pending: true, text: 'Hi there. ' } }))
    await settle()

    // The run finishes and the reply completes; the tail chunk is spoken too.
    await act(async () => update({ busy: false, response: { id: 'm1', pending: false, text: 'Hi there. Bye.' } }))
    await settle()
    await settle()

    // The whole point: the loop must open the mic again for the next turn.
    expect(micStart).toHaveBeenCalledTimes(2)
    expect(view.result.current.status).toBe('listening')
  })

  it('re-arms the mic when the run ends with nothing to speak', async () => {
    const { update, view } = renderConversation()

    await act(async () => update({ enabled: true }))
    await settle()
    expect(micStart).toHaveBeenCalledTimes(1)

    micStop.mockResolvedValueOnce({ audio: new Blob(['x']), durationMs: 900, heardSpeech: true })
    await act(async () => micOptions?.onSilence?.())
    expect(view.result.current.status).toBe('thinking')

    // The run ends without any visible assistant reply.
    await act(async () => update({ busy: false }))
    await settle()

    expect(micStart).toHaveBeenCalledTimes(2)
    expect(view.result.current.status).toBe('listening')
  })
})
