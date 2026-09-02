import type { ChildProcess } from 'node:child_process'
import { EventEmitter } from 'node:events'

import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  encodeRealtimeVoiceDelegationProgress,
  encodeRealtimeVoiceDelegationResult,
  parseRealtimeVoiceEvent,
  parseRealtimeVoicePhase,
  registerRealtimeVoiceProcess,
  stopRegisteredRealtimeVoiceProcess
} from '../domain/realtimeVoice.js'

afterEach(() => {
  vi.useRealTimers()
})

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

describe('realtime voice child supervision', () => {
  const childProcess = (exitOnInterrupt: boolean) => {
    const child = new EventEmitter() as EventEmitter & {
      exitCode: null | number
      kill: ReturnType<typeof vi.fn>
      signalCode: NodeJS.Signals | null
    }

    child.exitCode = null
    child.signalCode = null
    child.kill = vi.fn((signal: NodeJS.Signals) => {
      if (signal === 'SIGINT' && exitOnInterrupt) {
        child.signalCode = signal
        child.emit('exit', null, signal)
      }
      return true
    })

    return child as unknown as ChildProcess
  }

  it('stops the registered child once with SIGINT', async () => {
    const child = childProcess(true)
    registerRealtimeVoiceProcess(child)

    const first = stopRegisteredRealtimeVoiceProcess()
    const second = stopRegisteredRealtimeVoiceProcess()

    expect(first).toBe(second)
    await first
    expect(child.kill).toHaveBeenCalledTimes(1)
    expect(child.kill).toHaveBeenCalledWith('SIGINT')
  })

  it('escalates an unresponsive child to SIGKILL after the grace period', async () => {
    vi.useFakeTimers()
    const child = childProcess(false)
    registerRealtimeVoiceProcess(child)

    const stopped = stopRegisteredRealtimeVoiceProcess(25)
    await vi.advanceTimersByTimeAsync(25)
    await stopped

    expect(child.kill).toHaveBeenNthCalledWith(1, 'SIGINT')
    expect(child.kill).toHaveBeenNthCalledWith(2, 'SIGKILL')
  })
})
