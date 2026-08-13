/**
 * Contract: host.attachFileToComposer must land the attachment where the
 * composer will actually read it — the live scope atom when a main composer
 * is mounted, the per-session draft stash when it is not (Media Studio and
 * every other full-page surface). The stash path is the regression under
 * test: the original implementation wrote only to the atom, and the chat
 * route's mount-time draft restore clobbered it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $composerAttachments,
  clearSessionDraft,
  markMainComposerDraftScope,
  takeSessionDraft
} from '@/store/composer'

import { attachComposerFile } from './composer-attach'
import { isComposerMounted, markComposerMounted } from './composer-attach-presence'

const bridgeWindow = window as unknown as {
  hermesDesktop?: { readFileDataUrl?: (path: string, maxBytes?: number) => Promise<string> }
}

describe('composer-attach-presence', () => {
  it('counts overlapping mounts and releases idempotently', () => {
    expect(isComposerMounted('main')).toBe(false)

    const a = markComposerMounted('main')
    const b = markComposerMounted('main')

    expect(isComposerMounted('main')).toBe(true)
    a()
    expect(isComposerMounted('main')).toBe(true)
    a() // double release must not steal b's claim
    expect(isComposerMounted('main')).toBe(true)
    b()
    expect(isComposerMounted('main')).toBe(false)
  })
})

describe('attachComposerFile', () => {
  const originalBridge = bridgeWindow.hermesDesktop

  beforeEach(() => {
    $composerAttachments.set([])
    markMainComposerDraftScope('session-a')
    clearSessionDraft('session-a')
    bridgeWindow.hermesDesktop = {
      readFileDataUrl: vi.fn().mockResolvedValue('data:image/png;base64,x')
    }
  })

  afterEach(() => {
    bridgeWindow.hermesDesktop = originalBridge
    $composerAttachments.set([])
    clearSessionDraft('session-a')
  })

  it('returns false for an empty path', async () => {
    await expect(attachComposerFile('')).resolves.toBe(false)
  })

  it('stages into the live atom while a main composer is mounted', async () => {
    const release = markComposerMounted('main')

    try {
      await expect(attachComposerFile('/tmp/shot.png')).resolves.toBe(true)

      const live = $composerAttachments.get()

      expect(live).toHaveLength(1)
      expect(live[0]).toMatchObject({ kind: 'image', path: '/tmp/shot.png', previewUrl: 'data:image/png;base64,x' })
      // Nothing parked in the stash — the atom is authoritative while mounted.
      expect(takeSessionDraft('session-a').attachments).toHaveLength(0)
    } finally {
      release()
    }
  })

  it('stages into the session draft stash while no composer is mounted', async () => {
    await expect(attachComposerFile('/tmp/render.mp4')).resolves.toBe(true)

    expect($composerAttachments.get()).toHaveLength(0)

    const stashed = takeSessionDraft('session-a')

    expect(stashed.attachments).toHaveLength(1)
    expect(stashed.attachments[0]).toMatchObject({ kind: 'file', path: '/tmp/render.mp4' })
  })

  it('resolves image previews into the stash after the fact', async () => {
    await attachComposerFile('/tmp/late-preview.png')

    const stashed = takeSessionDraft('session-a')

    expect(stashed.attachments[0]?.previewUrl).toBe('data:image/png;base64,x')
  })

  it('preserves stashed draft text when staging an attachment', async () => {
    const { stashSessionDraft } = await import('@/store/composer')

    stashSessionDraft('session-a', 'half-typed thought', [])
    await attachComposerFile('/tmp/render.mp4')

    const stashed = takeSessionDraft('session-a')

    expect(stashed.text).toBe('half-typed thought')
    expect(stashed.attachments).toHaveLength(1)
  })
})
