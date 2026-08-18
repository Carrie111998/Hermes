import { afterEach, describe, expect, it } from 'vitest'

import { host } from '@/sdk/index'
import { setTtsSpeaking, setUserSpeaking } from '@/store/voice-lifecycle'
import { setVoicePlaybackState } from '@/store/voice-playback'

describe('host voice lifecycle', () => {
  afterEach(() => {
    setUserSpeaking(false)
    setTtsSpeaking(false)
  })

  it('exposes the current snapshot and provider-neutral transition events', () => {
    const events: string[] = []
    const off = host.onVoiceEvent('*', event => events.push(event.type))

    setUserSpeaking(true)
    setTtsSpeaking(true)

    expect(host.state.voiceLifecycle.get()).toEqual({ ttsSpeaking: true, userSpeaking: true })
    expect(events).toEqual(['user_speech_started', 'tts_started'])

    off()
  })

  it('follows authoritative playback state transitions', () => {
    const events: string[] = []
    const off = host.onVoiceEvent('*', event => events.push(event.type))

    setVoicePlaybackState({ audioElement: null, messageId: null, sequence: 1, source: 'read-aloud', status: 'speaking' })
    setVoicePlaybackState({ audioElement: null, messageId: null, sequence: 1, source: null, status: 'idle' })

    expect(events).toEqual(['tts_started', 'tts_ended'])

    off()
  })
})
