import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection, setApiRequestProfile } from '@/hermes'
import { $voicePlayback } from '@/store/voice-playback'

import { playSpeechText, resolveSpeakStreamUrl, startSpeechStream, stopVoicePlayback } from './voice-playback'

const { directTtsConfigMock, speakTextMock, synthesizeSpeechClientDirectMock } = vi.hoisted(() => ({
  directTtsConfigMock: vi.fn(),
  speakTextMock: vi.fn(),
  synthesizeSpeechClientDirectMock: vi.fn()
}))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  speakText: speakTextMock
}))

vi.mock('@/lib/voice-client-direct', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  directTtsConfig: directTtsConfigMock,
  synthesizeSpeechClientDirect: synthesizeSpeechClientDirectMock
}))

const DIRECT_TTS_CONFIG = {
  api_key: 'test-only',
  base_url: 'https://tts.invalid',
  mode: 'direct',
  model: null,
  provider: 'test',
  speed: null,
  voice: null,
  wire: 'openai-speech'
} as const

// The speak-stream WebSocket must dial the ACTIVE (connection, profile)
// backend — the same one chat and every REST audio call use. Before this
// contract was pinned it resolved through the bare v1 getConnection path,
// so a registry remote riding over a local install synthesized replies with
// the LOCAL machine's (often unconfigured) TTS while chat correctly went
// remote (desktop-remote voice report, Aug 2026).
describe('resolveSpeakStreamUrl', () => {
  const remoteWsUrl = 'wss://gateway.example/api/ws?ticket=fresh'
  const localWsUrl = 'ws://127.0.0.1:5151/api/ws?token=local'

  let getConnection: ReturnType<typeof vi.fn>
  let getConnectionFor: ReturnType<typeof vi.fn>
  let getGatewayWsUrl: ReturnType<typeof vi.fn>
  let getGatewayWsUrlFor: ReturnType<typeof vi.fn>

  beforeEach(() => {
    getConnection = vi.fn(async () => ({ authMode: 'token', baseUrl: 'http://127.0.0.1:5151', wsUrl: localWsUrl }))

    getConnectionFor = vi.fn(async () => ({
      authMode: 'token',
      baseUrl: 'https://gateway.example',
      wsUrl: remoteWsUrl
    }))

    getGatewayWsUrl = vi.fn(async () => ({ ok: true, wsUrl: localWsUrl }))
    getGatewayWsUrlFor = vi.fn(async () => ({ ok: true, wsUrl: remoteWsUrl }))

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { getConnection, getConnectionFor, getGatewayWsUrl, getGatewayWsUrlFor }
    })
  })

  afterEach(() => {
    setApiRequestConnection(null)
    setApiRequestProfile(null)
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.useRealTimers()
  })

  it('resolves through the registry (connection, profile) bridges when a registry connection is active', async () => {
    setApiRequestConnection('gw-tailscale')
    setApiRequestProfile('research')

    const url = await resolveSpeakStreamUrl()

    expect(url).toContain('wss://gateway.example')
    expect(url).toContain('/api/audio/speak-stream')
    expect(getConnectionFor).toHaveBeenCalledWith({ connectionId: 'gw-tailscale', profile: 'research' })
    expect(getGatewayWsUrlFor).toHaveBeenCalledWith({ connectionId: 'gw-tailscale', profile: 'research' })
    // The v1 primary path must NOT be consulted — that's the local machine.
    expect(getConnection).not.toHaveBeenCalled()
    expect(getGatewayWsUrl).not.toHaveBeenCalled()
  })

  it('uses an explicit session owner instead of ambient routing', async () => {
    setApiRequestConnection('ambient-source')
    setApiRequestProfile('ambient-profile')

    await resolveSpeakStreamUrl({ connectionId: 'tile-source', profile: 'tile-profile' })

    expect(getConnectionFor).toHaveBeenCalledWith({ connectionId: 'tile-source', profile: 'tile-profile' })
    expect(getGatewayWsUrlFor).toHaveBeenCalledWith({ connectionId: 'tile-source', profile: 'tile-profile' })
  })

  it('keeps the legacy profile path byte-identical when no registry connection is active', async () => {
    setApiRequestProfile('coder')

    const url = await resolveSpeakStreamUrl()

    expect(url).toContain('ws://127.0.0.1:5151')
    expect(url).toContain('/api/audio/speak-stream')
    expect(url).toContain('profile=coder')
    expect(getConnection).toHaveBeenCalledWith('coder')
    expect(getConnectionFor).not.toHaveBeenCalled()
  })

  it('preserves a backend-namespace profile already minted into the ws URL', async () => {
    // SSH remoteProfile aliasing / sharedRemote scoping: the registry mint
    // writes the BACKEND's profile name into the URL. The desktop-side
    // routing alias must not overwrite it.
    setApiRequestConnection('gw-ssh')
    setApiRequestProfile('mara')
    getGatewayWsUrlFor.mockResolvedValue({
      ok: true,
      wsUrl: 'wss://gateway.example/api/ws?ticket=fresh&profile=default'
    })

    const url = await resolveSpeakStreamUrl()

    expect(url).toContain('profile=default')
    expect(url).not.toContain('profile=mara')
  })

  it('falls back to the plain connection descriptor when the *For bridges are absent (older main)', async () => {
    setApiRequestConnection('gw-tailscale')
    setApiRequestProfile('research')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { getConnection, getGatewayWsUrl }
    })

    const url = await resolveSpeakStreamUrl()

    // Best available answer without the bridges: the profile-scoped pool
    // descriptor's own wsUrl (no cross-scope re-mint).
    expect(url).toContain('/api/audio/speak-stream')
    expect(getConnection).toHaveBeenCalledWith('research')
  })

  it('resolves to null instead of hanging forever when getConnection() wedges (#93454)', async () => {
    // desktop.getConnection/getConnectionFor/resolveGatewayWsUrl are IPC
    // round-trips into the main process with no timeout of their own. A
    // wedged main-process round-trip otherwise hangs voice mode's "speaking"
    // state forever instead of falling back to playSpeechText.
    vi.useFakeTimers()
    setApiRequestProfile('coder')
    getConnection.mockImplementation(() => new Promise(() => undefined))

    const pending = resolveSpeakStreamUrl()

    await vi.advanceTimersByTimeAsync(20_000)

    await expect(pending).resolves.toBeNull()
  })
})

describe('startSpeechStream owner routing', () => {
  afterEach(() => {
    stopVoicePlayback()
    Reflect.deleteProperty(window, 'hermesDesktop')
    directTtsConfigMock.mockReset()
    speakTextMock.mockReset()
    vi.unstubAllGlobals()
  })

  it('binds direct TTS discovery to the supplied session owner', async () => {
    const ownerRoute = { connectionId: 'tile-source', profile: 'tile-profile' }

    directTtsConfigMock.mockResolvedValueOnce(null)
    Reflect.deleteProperty(window, 'hermesDesktop')

    await startSpeechStream({ ownerRoute, source: 'voice-conversation' })

    expect(directTtsConfigMock).toHaveBeenCalledWith(ownerRoute)
  })

  it('binds one-shot direct TTS discovery to the supplied session owner', async () => {
    const ownerRoute = { connectionId: 'tile-source', profile: 'tile-profile' }

    directTtsConfigMock.mockResolvedValueOnce(null)
    speakTextMock.mockRejectedValueOnce(new Error('stop after routing assertion'))
    Reflect.deleteProperty(window, 'hermesDesktop')

    await expect(playSpeechText('tile reply', { ownerRoute, source: 'read-aloud' })).rejects.toThrow(
      'stop after routing assertion'
    )

    expect(directTtsConfigMock).toHaveBeenCalledWith(ownerRoute)
    expect(speakTextMock).toHaveBeenCalledWith('tile reply', ownerRoute)
  })

  it('binds one-shot WebSocket TTS discovery to the supplied session owner', async () => {
    const ownerRoute = { connectionId: 'tile-source', profile: 'tile-profile' }

    const getConnection = vi.fn(async () => ({
      authMode: 'token',
      baseUrl: 'https://ambient.invalid',
      wsUrl: 'wss://ambient.invalid/api/ws?ticket=ambient'
    }))

    const getConnectionFor = vi.fn(async () => ({
      authMode: 'token',
      baseUrl: 'https://tile.invalid',
      wsUrl: 'wss://tile.invalid/api/ws?ticket=tile'
    }))

    const getGatewayWsUrlFor = vi.fn(async () => ({ ok: true, wsUrl: 'wss://tile.invalid/api/ws?ticket=tile' }))

    class PendingWebSocket {
      static CONNECTING = 0
      static OPEN = 1
      binaryType = ''
      readyState = PendingWebSocket.CONNECTING
      close = vi.fn()
      send = vi.fn()
    }

    directTtsConfigMock.mockResolvedValueOnce(null)
    vi.stubGlobal('WebSocket', PendingWebSocket)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { getConnection, getConnectionFor, getGatewayWsUrlFor }
    })

    const playback = playSpeechText('tile reply', { ownerRoute, source: 'read-aloud' })

    await vi.waitFor(() => expect(directTtsConfigMock).toHaveBeenCalled())
    await Promise.resolve()
    await Promise.resolve()

    expect(getConnectionFor).toHaveBeenCalledWith(ownerRoute)
    expect(getConnection).not.toHaveBeenCalled()
    stopVoicePlayback()
    await expect(playback).resolves.toBe(false)
  })

  it('binds WebSocket TTS discovery to the supplied session owner', async () => {
    const ownerRoute = { connectionId: 'tile-source', profile: 'tile-profile' }

    const getConnection = vi.fn(async () => ({
      authMode: 'token',
      baseUrl: 'https://ambient.invalid',
      wsUrl: 'wss://ambient.invalid/api/ws?ticket=ambient'
    }))

    const getConnectionFor = vi.fn(async () => ({
      authMode: 'token',
      baseUrl: 'https://tile.invalid',
      wsUrl: 'wss://tile.invalid/api/ws?ticket=tile'
    }))

    const getGatewayWsUrlFor = vi.fn(async () => ({ ok: true, wsUrl: 'wss://tile.invalid/api/ws?ticket=tile' }))

    class PendingWebSocket {
      static CONNECTING = 0
      static OPEN = 1
      binaryType = ''
      readyState = PendingWebSocket.CONNECTING
      close = vi.fn()
      send = vi.fn()
    }

    directTtsConfigMock.mockResolvedValueOnce(null)
    vi.stubGlobal('WebSocket', PendingWebSocket)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { getConnection, getConnectionFor, getGatewayWsUrlFor }
    })

    const session = await startSpeechStream({ ownerRoute, source: 'voice-conversation' })

    expect(session).not.toBeNull()
    expect(getConnectionFor).toHaveBeenCalledWith(ownerRoute)
    expect(getConnection).not.toHaveBeenCalled()
    stopVoicePlayback()
  })
})

describe('startSpeechStream setup cancellation', () => {
  afterEach(() => {
    stopVoicePlayback()
    directTtsConfigMock.mockReset()
    speakTextMock.mockReset()
    Reflect.deleteProperty(window, 'hermesDesktop')
    vi.unstubAllGlobals()
  })

  it('cancels one-shot playback setup when its caller generation becomes stale', async () => {
    let resolveConfig: ((config: null) => void) | undefined

    const pendingConfig = new Promise<null>(resolve => {
      resolveConfig = resolve
    })

    let current = true

    directTtsConfigMock.mockReturnValueOnce(pendingConfig)
    speakTextMock.mockRejectedValueOnce(new Error('stale playback reached relay'))

    const playback = playSpeechText('stale reply', {
      isCurrent: () => current,
      source: 'read-aloud'
    })

    current = false
    resolveConfig?.(null)

    await expect(playback).resolves.toBe(false)
    expect(speakTextMock).not.toHaveBeenCalled()
    expect($voicePlayback.get().status).toBe('idle')
  })

  it('clears preparing when caller generation expires during relay synthesis', async () => {
    let resolveSpeech: ((response: { data_url: string }) => void) | undefined

    const pendingSpeech = new Promise<{ data_url: string }>(resolve => {
      resolveSpeech = resolve
    })

    let current = true

    directTtsConfigMock.mockResolvedValueOnce(null)
    speakTextMock.mockReturnValueOnce(pendingSpeech)
    Reflect.deleteProperty(window, 'hermesDesktop')

    const playback = playSpeechText('stale relay reply', {
      isCurrent: () => current,
      source: 'read-aloud'
    })

    await vi.waitFor(() => expect(speakTextMock).toHaveBeenCalled())
    current = false
    resolveSpeech?.({ data_url: 'data:audio/mpeg;base64,AA==' })

    await expect(playback).resolves.toBe(false)
    expect($voicePlayback.get().status).toBe('idle')
  })

  it('does not disturb newer playback when stale discovery resolves', async () => {
    let resolveConfig: ((config: unknown) => void) | undefined

    const pendingConfig = new Promise(resolve => {
      resolveConfig = resolve
    })

    let current = true
    const sequenceBefore = $voicePlayback.get().sequence

    directTtsConfigMock.mockReturnValueOnce(pendingConfig)
    const session = startSpeechStream({ isCurrent: () => current, source: 'voice-conversation' })

    current = false
    resolveConfig?.(DIRECT_TTS_CONFIG)

    await expect(session).resolves.toBeNull()
    expect($voicePlayback.get().sequence).toBe(sequenceBefore)
  })
})

describe('startSpeechStream playback ownership', () => {
  afterEach(() => {
    stopVoicePlayback()
    setApiRequestConnection(null)
    setApiRequestProfile(null)
    Reflect.deleteProperty(window, 'hermesDesktop')
    directTtsConfigMock.mockReset()
    speakTextMock.mockReset()
    synthesizeSpeechClientDirectMock.mockReset()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('does not publish idle when a replaced client-direct session settles', async () => {
    class PendingAudio extends EventTarget {
      src = ''
      load = vi.fn()
      pause = vi.fn()
      play = vi.fn(() => new Promise<void>(() => undefined))
    }

    class TestUrl extends URL {
      static createObjectURL = vi.fn(() => 'blob:test-audio')
      static revokeObjectURL = vi.fn()
    }

    vi.stubGlobal('Audio', PendingAudio)
    vi.stubGlobal('URL', TestUrl)
    directTtsConfigMock.mockResolvedValue(DIRECT_TTS_CONFIG)
    synthesizeSpeechClientDirectMock.mockResolvedValue(new ArrayBuffer(4))

    const first = await startSpeechStream({ messageId: 'first', source: 'voice-conversation' })

    first?.append('First sentence.')
    first?.finish()
    await vi.waitFor(() => expect($voicePlayback.get().status).toBe('speaking'))

    await startSpeechStream({ messageId: 'replacement', source: 'voice-conversation' })
    await Promise.resolve()

    expect($voicePlayback.get()).toMatchObject({ messageId: 'replacement', status: 'preparing' })
  })

  it('does not publish idle when a replaced WebSocket session settles', async () => {
    const sockets: FakeWebSocket[] = []

    class FakeWebSocket {
      static CONNECTING = 0
      static OPEN = 1
      binaryType = ''
      onclose: null | (() => void) = null
      onerror: null | (() => void) = null
      onmessage: null | ((event: { data: ArrayBuffer | string }) => void) = null
      onopen: null | (() => void) = null
      readyState = FakeWebSocket.OPEN
      close = vi.fn()
      send = vi.fn()

      constructor() {
        sockets.push(this)
      }
    }

    class FakeAudioContext {
      currentTime = 0
      destination = {}
      state = 'running'
      close = vi.fn(async () => undefined)
      createBuffer = vi.fn((_channels: number, length: number, sampleRate: number) => ({
        duration: length / sampleRate,
        getChannelData: () => new Float32Array(length)
      }))
      createBufferSource = vi.fn(() => ({ connect: vi.fn(), start: vi.fn(), buffer: null }))
      resume = vi.fn(async () => undefined)
    }

    vi.stubGlobal('WebSocket', FakeWebSocket)
    vi.stubGlobal('AudioContext', FakeAudioContext)
    directTtsConfigMock.mockResolvedValue(null)
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnection: vi.fn(async () => ({
          authMode: 'token',
          baseUrl: 'https://gateway.example',
          wsUrl: 'wss://gateway.example/api/ws?ticket=fresh'
        })),
        getGatewayWsUrl: vi.fn(async () => ({
          ok: true,
          wsUrl: 'wss://gateway.example/api/ws?ticket=fresh'
        }))
      }
    })

    await startSpeechStream({ messageId: 'first', source: 'voice-conversation' })
    sockets[0].onmessage?.({ data: JSON.stringify({ sample_rate: 24_000, type: 'start' }) })
    sockets[0].onmessage?.({ data: new Uint8Array([1, 0]).buffer })
    expect($voicePlayback.get().status).toBe('speaking')

    await startSpeechStream({ messageId: 'replacement', source: 'voice-conversation' })
    await Promise.resolve()

    expect($voicePlayback.get()).toMatchObject({ messageId: 'replacement', status: 'preparing' })
  })

  it('does not publish idle when data-URL playback is replaced after its final ownership check', async () => {
    const audios: EndingAudio[] = []

    class EndingAudio extends EventTarget {
      src: string
      load = vi.fn()
      pause = vi.fn()
      play = vi.fn(async () => undefined)

      constructor(src: string) {
        super()
        this.src = src
        audios.push(this)
      }
    }

    vi.stubGlobal('Audio', EndingAudio)
    Reflect.deleteProperty(window, 'hermesDesktop')
    directTtsConfigMock.mockResolvedValueOnce(null).mockReturnValueOnce(new Promise(() => undefined))
    speakTextMock.mockResolvedValue({ data_url: 'data:audio/mpeg;base64,AA==' })

    const first = playSpeechText('first reply', { messageId: 'first', source: 'read-aloud' })

    await vi.waitFor(() => expect($voicePlayback.get().status).toBe('speaking'))
    audios[0].dispatchEvent(new Event('ended'))
    queueMicrotask(() => {
      void playSpeechText('replacement reply', { messageId: 'replacement', source: 'read-aloud' })
    })

    await expect(first).resolves.toBe(true)

    expect($voicePlayback.get()).toMatchObject({ messageId: 'replacement', status: 'preparing' })
  })
})
