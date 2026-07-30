import { afterEach, describe, expect, it, vi } from 'vitest'

interface WorkletPort {
  onmessage: ((event: MessageEvent) => void) | null
  postMessage: ReturnType<typeof vi.fn>
}

class TestAudioWorkletProcessor {
  port: WorkletPort

  constructor() {
    this.port = {
      onmessage: null,
      postMessage: vi.fn()
    }
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('HermesVoicePlayoutProcessor', () => {
  it('acknowledges an empty terminal drain while the clock is paused', async () => {
    let registered: typeof TestAudioWorkletProcessor | undefined

    vi.stubGlobal('AudioWorkletProcessor', TestAudioWorkletProcessor)
    vi.stubGlobal('registerProcessor', (_name: string, processor: typeof TestAudioWorkletProcessor) => {
      registered = processor
    })

    // @ts-expect-error The worklet is a browser-loaded JavaScript module and
    // intentionally has no renderer-side declaration file.
    const { HermesVoicePlayoutProcessor } = await import('../../public/audio-worklet/voice-playout-processor.js')
    expect(registered).toBe(HermesVoicePlayoutProcessor)

    const processor = new HermesVoicePlayoutProcessor({ processorOptions: {} }) as unknown as {
      port: WorkletPort
      process: (inputs: unknown[], outputs: Float32Array[][]) => boolean
    }

    processor.port.onmessage?.({ data: { id: 17, type: 'drain' } } as MessageEvent)

    expect(processor.process([], [[new Float32Array(128)]])).toBe(true)
    expect(processor.port.postMessage).toHaveBeenCalledWith({ id: 17, type: 'drained' })
  })
})
