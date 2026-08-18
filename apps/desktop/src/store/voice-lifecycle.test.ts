import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  $voiceLifecycle,
  onVoiceLifecycleEvent,
  setTtsSpeaking,
  setUserSpeaking,
  type VoiceLifecycleEventType
} from './voice-lifecycle'

describe('voice lifecycle', () => {
  afterEach(() => {
    setUserSpeaking(false)
    setTtsSpeaking(false)
  })

  it('emits each transition once and keeps a current snapshot', () => {
    const events: VoiceLifecycleEventType[] = []
    const off = onVoiceLifecycleEvent('*', event => events.push(event.type))

    setUserSpeaking(true)
    setUserSpeaking(true)
    setTtsSpeaking(true)
    setUserSpeaking(false)
    setTtsSpeaking(false)

    expect(events).toEqual(['user_speech_started', 'tts_started', 'user_speech_ended', 'tts_ended'])
    expect($voiceLifecycle.get()).toEqual({ ttsSpeaking: false, userSpeaking: false })

    off()
  })

  it('isolates listener failures and disposes subscriptions', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const listener = vi.fn()

    const offFailing = onVoiceLifecycleEvent('tts_started', () => {
      throw new Error('plugin failure')
    })

    const offListener = onVoiceLifecycleEvent('tts_started', listener)

    setTtsSpeaking(true)
    expect(listener).toHaveBeenCalledTimes(1)
    expect(error).toHaveBeenCalledTimes(1)

    offFailing()
    offListener()
    setTtsSpeaking(false)
    setTtsSpeaking(true)
    expect(listener).toHaveBeenCalledTimes(1)

    error.mockRestore()
  })

  it('keeps speech active while another VAD source still owns it', () => {
    const events: VoiceLifecycleEventType[] = []
    const off = onVoiceLifecycleEvent('*', event => events.push(event.type))
    const firstCapture = Symbol('voice-capture')
    const secondCapture = Symbol('voice-capture')

    setUserSpeaking(true, firstCapture)
    setUserSpeaking(true, secondCapture)
    setUserSpeaking(false, firstCapture)

    expect($voiceLifecycle.get().userSpeaking).toBe(true)
    expect(events).toEqual(['user_speech_started'])

    setUserSpeaking(false, secondCapture)
    expect(events).toEqual(['user_speech_started', 'user_speech_ended'])

    off()
  })
})
