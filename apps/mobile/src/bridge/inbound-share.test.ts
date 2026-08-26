import { beforeEach, describe, expect, it, vi } from 'vitest'

const { inboundShare } = vi.hoisted(() => ({
  inboundShare: {
    getPending: vi.fn(),
    readItem: vi.fn(),
  },
}))

vi.mock('@capacitor/core', () => ({
  registerPlugin: () => inboundShare,
}))

import { consumePendingInboundShare } from './inbound-share'

describe('inbound Android shares', () => {
  beforeEach(() => {
    inboundShare.getPending.mockReset()
    inboundShare.readItem.mockReset()
  })

  it('turns explicitly shared URI-grant bytes into browser Files without retaining the source URI', async () => {
    inboundShare.getPending.mockResolvedValue({
      items: [{ id: 'share-1', mimeType: 'text/plain', name: 'notes.txt' }],
      text: 'Please review this.',
    })
    inboundShare.readItem.mockResolvedValue({ base64: 'c2hhcmVkIGJ5dGVz', mimeType: 'text/plain', name: 'notes.txt' })

    const share = await consumePendingInboundShare()

    expect(share.text).toBe('Please review this.')
    expect(share.files).toHaveLength(1)
    expect(share.files[0]?.name).toBe('notes.txt')
    await expect(share.files[0]?.text()).resolves.toBe('shared bytes')
    expect(inboundShare.readItem).toHaveBeenCalledWith({ id: 'share-1' })
  })

  it('ignores malformed or oversized-unavailable share items without dropping shared text', async () => {
    inboundShare.getPending.mockResolvedValue({ items: [{ id: 'missing', name: 'missing.bin' }], text: 'Keep this note.' })
    inboundShare.readItem.mockRejectedValue(new Error('Shared file is unavailable.'))

    await expect(consumePendingInboundShare()).resolves.toEqual({ files: [], text: 'Keep this note.' })
  })
})
