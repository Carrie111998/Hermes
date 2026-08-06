import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestProfile, transcribeAudio } from './hermes'

describe('transcribeAudio preview routing', () => {
  const api = vi.fn()
  const getConnectionConfig = vi.fn()

  beforeEach(() => {
    api.mockReset()
    getConnectionConfig.mockReset()

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { api, getConnectionConfig }
    })
  })

  afterEach(() => {
    setApiRequestProfile(null)
  })

  it('fails closed without sending preview audio when the backend is remote', async () => {
    getConnectionConfig.mockResolvedValue({ mode: 'remote' })

    await expect(
      transcribeAudio('data:audio/webm;base64,cHJpdmF0ZQ==', 'audio/webm', {
        localOnly: true,
        previewOnly: true
      })
    ).rejects.toThrow('local Hermes backend')

    expect(api).not.toHaveBeenCalled()
  })

  it('validates and sends preview audio through the same local profile', async () => {
    setApiRequestProfile('work')
    getConnectionConfig.mockResolvedValue({ mode: 'local' })
    api.mockResolvedValue({ text: '부분 문장' })

    await expect(
      transcribeAudio('data:audio/webm;base64,bG9jYWw=', 'audio/webm', {
        localOnly: true,
        previewOnly: true
      })
    ).resolves.toEqual({ text: '부분 문장' })

    expect(getConnectionConfig).toHaveBeenCalledWith('work')
    expect(api).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.objectContaining({ local_only: true, preview_only: true }),
        path: '/api/audio/transcribe',
        profile: 'work',
        requireLocalBackend: true
      })
    )
  })
})
