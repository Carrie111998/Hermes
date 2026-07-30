import { describe, expect, it } from 'vitest'

import { negotiateVoiceStreamProtocol } from './voice-playback'

describe('voice stream negotiation', () => {
  it('selects the provider-neutral protocol only from an explicit v1 start frame', () => {
    expect(negotiateVoiceStreamProtocol({ protocol: 'hermes.audio.v1' })).toBe('hermes.audio.v1')
    expect(negotiateVoiceStreamProtocol({ protocol_version: 'hermes.audio.v1' })).toBe('hermes.audio.v1')
  })

  it('keeps older raw-PCM gateways on the legacy path', () => {
    expect(negotiateVoiceStreamProtocol({ sample_rate: 24_000 })).toBe('legacy')
    expect(negotiateVoiceStreamProtocol({ encoding: 'pcm_s16le', version: 1 })).toBe('legacy')
    expect(negotiateVoiceStreamProtocol({ protocol: 'hermes.audio.v0' })).toBe('legacy')
  })
})
