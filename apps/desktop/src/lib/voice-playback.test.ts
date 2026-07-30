import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { negotiateVoiceStreamProtocol, startSpeechStream, stopVoicePlayback } from './voice-playback'

class FakeWebSocket {
  static readonly OPEN = 1
  static readonly CONNECTING = 0
  static readonly CLOSED = 3
  static latest: FakeWebSocket | null = null

  readonly sent: string[] = []
  readonly url: string
  readyState = FakeWebSocket.OPEN
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: { data: ArrayBuffer | string }) => void) | null = null
  onopen: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.latest = this
  }

  send(data: string) {
    this.sent.push(data)
  }

  close() {
    if (this.readyState === FakeWebSocket.CLOSED) {
      return
    }

    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.()
  }

  emitJson(frame: object) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  emitBinary(data: ArrayBuffer) {
    this.onmessage?.({ data })
  }
}

class FakeAudioWorkletNode {
  static latest: FakeAudioWorkletNode | null = null
  readonly port = {
    onmessage: null as ((event: { data: { id?: number; type?: string } }) => void) | null,
    postMessage: vi.fn((message: { id?: number; type?: string }) => {
      if (message.type === 'drain') {
        this.port.onmessage?.({ data: { id: message.id, type: 'drained' } })
      }
    })
  }
  readonly connect = vi.fn()
  readonly disconnect = vi.fn()

  constructor() {
    FakeAudioWorkletNode.latest = this
  }
}

class FakeAudioContext {
  static latest: FakeAudioContext | null = null
  readonly audioWorklet = { addModule: vi.fn(async () => undefined) }
  readonly destination = {}
  readonly sampleRate: number
  readonly state = 'running'
  readonly currentTime = 0
  readonly close = vi.fn(async () => undefined)
  readonly resume = vi.fn(async () => undefined)
  readonly createBuffer = vi.fn((_channels: number, sampleCount: number, sampleRate: number) => ({
    duration: sampleCount / sampleRate,
    getChannelData: () => new Float32Array(sampleCount)
  }))
  readonly createBufferSource = vi.fn(() => ({
    buffer: null as unknown,
    connect: vi.fn(),
    start: vi.fn()
  }))

  constructor(options?: { sampleRate?: number }) {
    this.sampleRate = options?.sampleRate ?? 24_000
    FakeAudioContext.latest = this
  }
}

const desktopWindow = window as unknown as { hermesDesktop?: unknown }

beforeEach(() => {
  FakeWebSocket.latest = null
  FakeAudioWorkletNode.latest = null
  FakeAudioContext.latest = null
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.stubGlobal('AudioContext', FakeAudioContext)
  vi.stubGlobal('AudioWorkletNode', FakeAudioWorkletNode)
  desktopWindow.hermesDesktop = {
    getConnection: vi.fn(async () => ({ wsUrl: 'ws://gateway/api/ws', authMode: 'token' }))
  }
})

afterEach(() => {
  stopVoicePlayback()
  FakeWebSocket.latest = null
  FakeAudioWorkletNode.latest = null
  FakeAudioContext.latest = null
  delete desktopWindow.hermesDesktop
  vi.unstubAllGlobals()
})

async function waitForWorklet(): Promise<FakeAudioWorkletNode> {
  await vi.waitFor(() => expect(FakeAudioWorkletNode.latest).not.toBeNull())

  return FakeAudioWorkletNode.latest as FakeAudioWorkletNode
}

function framePayload(sampleCount = 480): ArrayBuffer {
  return new Int16Array(sampleCount).buffer
}

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

describe('voice stream WebSocket playback', () => {
  it('consumes v1 start/metadata/binary/end frames through the AudioWorklet sink', async () => {
    const session = await startSpeechStream({ source: 'voice-conversation' })
    expect(session).not.toBeNull()
    const socket = FakeWebSocket.latest as FakeWebSocket
    expect(socket.url).toBe('ws://gateway/api/audio/speak-stream')

    socket.emitJson({
      channels: 1,
      encoding: 'pcm_s16le',
      initial_buffer_ms: 0,
      max_buffer_ms: 1_000,
      protocol: 'hermes.audio.v1',
      sample_rate: 24_000,
      type: 'start'
    })
    const node = await waitForWorklet()
    session?.append('Hello there.')
    session?.finish()

    socket.emitJson({
      sample_count: 480,
      sample_offset: 0,
      sequence: 0,
      type: 'audio'
    })
    socket.emitBinary(framePayload())
    socket.emitJson({ type: 'end' })

    await expect(session?.done).resolves.toBe('done')
    expect(node.port.postMessage).toHaveBeenCalledWith({ type: 'start' })
    expect(node.port.postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: 'write' }), expect.anything())
    expect(node.port.postMessage).toHaveBeenCalledWith(expect.objectContaining({ type: 'drain' }))
    expect(socket.sent.map(data => JSON.parse(data))).toEqual([
      { text: 'Hello there.' },
      { done: true }
    ])
  })

  it('falls back when a v1 stream reports an error before producing audio', async () => {
    const session = await startSpeechStream({ source: 'voice-conversation' })
    expect(session).not.toBeNull()
    const socket = FakeWebSocket.latest as FakeWebSocket

    socket.emitJson({
      channels: 1,
      encoding: 'pcm_s16le',
      initial_buffer_ms: 0,
      max_buffer_ms: 1_000,
      protocol: 'hermes.audio.v1',
      sample_rate: 24_000,
      type: 'start'
    })
    await waitForWorklet()
    socket.emitJson({ code: 'synthesis_failed', type: 'error' })

    await expect(session?.done).resolves.toBe('fallback')
  })

  it('plays legacy raw PCM received before any v1 start frame', async () => {
    vi.useFakeTimers()

    try {
      const session = await startSpeechStream({ source: 'voice-conversation' })
      expect(session).not.toBeNull()
      const socket = FakeWebSocket.latest as FakeWebSocket

      socket.emitBinary(framePayload())

      const context = FakeAudioContext.latest as FakeAudioContext
      expect(context.createBuffer).toHaveBeenCalledWith(1, 480, 24_000)
      const source = context.createBufferSource.mock.results[0]?.value
      expect(source?.start).toHaveBeenCalled()
      expect(FakeAudioWorkletNode.latest).toBeNull()

      socket.close()
      await vi.runAllTimersAsync()
      await expect(session?.done).resolves.toBe('done')
    } finally {
      vi.useRealTimers()
    }
  })
})
