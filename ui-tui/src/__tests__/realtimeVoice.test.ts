import { describe, expect, it } from 'vitest'

import {
  encodeRealtimeVoiceDelegationProgress,
  encodeRealtimeVoiceDelegationResult,
  parseRealtimeVoiceEvent,
  parseRealtimeVoicePhase
} from '../domain/realtimeVoice.js'

describe('realtime voice lifecycle', () => {
  it.each(['listening', 'solving', 'composing'] as const)('parses the %s phase', phase => {
    expect(parseRealtimeVoicePhase(`talk: state ${phase}`)).toBe(phase)
  })

  it('ignores transcripts and unknown states', () => {
    expect(parseRealtimeVoicePhase('hello from the model')).toBeNull()
    expect(parseRealtimeVoicePhase('talk: state disconnected')).toBeNull()
  })

  it('parses framed live transcripts without mistaking model text for control data', () => {
    expect(
      parseRealtimeVoiceEvent(
        'talk: event {"type":"transcript","role":"user","text":"check the weather","final":true}'
      )
    ).toEqual({
      type: 'transcript',
      role: 'user',
      text: 'check the weather',
      final: true
    })
    expect(
      parseRealtimeVoiceEvent('talk: event {"type":"error","message":"connection closed"}')
    ).toEqual({
      type: 'error',
      message: 'connection closed'
    })
    expect(parseRealtimeVoiceEvent('{"type":"transcript","role":"user"}')).toBeNull()
  })

  it('round-trips a delegated text-agent result over child stdin', () => {
    expect(
      parseRealtimeVoiceEvent('talk: event {"type":"delegate","id":"call-1","request":"inspect the bug"}')
    ).toEqual({
      type: 'delegate',
      id: 'call-1',
      request: 'inspect the bug'
    })
    expect(JSON.parse(encodeRealtimeVoiceDelegationResult('call-1', 'fixed'))).toEqual({
      type: 'delegate.result',
      id: 'call-1',
      output: 'fixed'
    })
    expect(JSON.parse(encodeRealtimeVoiceDelegationProgress('call-1', 'checking tests'))).toEqual({
      type: 'delegate.progress',
      id: 'call-1',
      text: 'checking tests'
    })
  })
})
