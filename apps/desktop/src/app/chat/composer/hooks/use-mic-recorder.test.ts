import { describe, expect, it } from 'vitest'

import { isUsableMicRecording } from './use-mic-recorder'

describe('isUsableMicRecording', () => {
  it('rejects a header-only recording stopped before MediaRecorder can finalize', () => {
    const audio = new Blob([new Uint8Array(128)], { type: 'audio/webm' })

    expect(isUsableMicRecording(audio, 80)).toBe(false)
  })

  it('accepts a finalized recording', () => {
    const audio = new Blob([new Uint8Array(1_024)], { type: 'audio/webm' })

    expect(isUsableMicRecording(audio, 500)).toBe(true)
  })
})
