import assert from 'node:assert/strict'

import { describe, expect, it, vi } from 'vitest'

import {
  installPreviewWebviewGuards,
  previewWindowOpenDecision,
  shouldHandleEscapeAsPreviewBack,
  wirePreviewWebviewById,
  wirePreviewWebviewContents
} from './preview-webview'

function mockGuest(session: object, id?: number) {
  return {
    id,
    session,
    canGoBack: () => false,
    getType: () => 'webview',
    goBack: vi.fn(),
    isDestroyed: () => false,
    loadURL: vi.fn(async () => undefined),
    on: vi.fn(),
    once: vi.fn(),
    setWindowOpenHandler: vi.fn()
  }
}

describe('previewWindowOpenDecision', () => {
  it('keeps attachment / target=_blank navigations inside the preview webview', () => {
    assert.equal(previewWindowOpenDecision('file:///tmp/report/shot.png'), 'navigate-in-place')
    assert.equal(previewWindowOpenDecision('blob:file:///uuid'), 'navigate-in-place')
    assert.equal(previewWindowOpenDecision('https://example.com'), 'navigate-in-place')
  })
})

describe('shouldHandleEscapeAsPreviewBack', () => {
  it('handles Escape only when the guest can go back', () => {
    expect(shouldHandleEscapeAsPreviewBack({ type: 'keyDown', key: 'Escape' }, true)).toBe(true)
    expect(shouldHandleEscapeAsPreviewBack({ type: 'keyDown', key: 'Escape' }, false)).toBe(false)
  })

  it('ignores non-Escape keys and keyUp', () => {
    expect(shouldHandleEscapeAsPreviewBack({ type: 'keyDown', key: 'Enter' }, true)).toBe(false)
    expect(shouldHandleEscapeAsPreviewBack({ type: 'keyUp', key: 'Escape' }, true)).toBe(false)
  })
})

describe('wirePreviewWebviewContents', () => {
  it('navigates in-place on window.open and goes back on Escape', async () => {
    const loads: string[] = []
    let openHandler: ((details: { url: string }) => { action: string }) | null = null

    let inputHandler: ((event: { preventDefault: () => void }, input: { type: string; key: string }) => void) | null =
      null

    let canGoBack = false

    const goBack = vi.fn(() => {
      // history is still non-empty for a second Esc until navigate completes
    })

    const contents = {
      canGoBack: () => canGoBack,
      goBack,
      isDestroyed: () => false,
      loadURL: async (url: string) => {
        loads.push(url)
        canGoBack = true
      },
      on: (event: string, listener: (...args: any[]) => void) => {
        if (event === 'before-input-event') {
          inputHandler = listener
        }
      },
      setWindowOpenHandler: (handler: (details: { url: string }) => { action: string }) => {
        openHandler = handler
      }
    }

    wirePreviewWebviewContents(contents as never)

    expect(openHandler).toBeTruthy()
    expect(openHandler!({ url: 'file:///tmp/shot.png' })).toEqual({ action: 'deny' })

    await vi.waitFor(() => {
      expect(loads).toEqual(['file:///tmp/shot.png'])
    })
    expect(canGoBack).toBe(true)

    const preventDefault = vi.fn()
    inputHandler?.({ preventDefault }, { type: 'keyDown', key: 'Escape' })

    expect(preventDefault).toHaveBeenCalledTimes(1)
    expect(goBack).toHaveBeenCalledTimes(1)
  })

  it('does not go back on Escape when history is empty', () => {
    const goBack = vi.fn()

    let inputHandler: ((event: { preventDefault: () => void }, input: { type: string; key: string }) => void) | null =
      null

    wirePreviewWebviewContents({
      canGoBack: () => false,
      goBack,
      isDestroyed: () => false,
      loadURL: async () => undefined,
      on: (event: string, listener: (...args: any[]) => void) => {
        if (event === 'before-input-event') {
          inputHandler = listener
        }
      },
      setWindowOpenHandler: () => undefined
    } as never)

    const preventDefault = vi.fn()
    inputHandler?.({ preventDefault }, { type: 'keyDown', key: 'Escape' })

    expect(preventDefault).not.toHaveBeenCalled()
    expect(goBack).not.toHaveBeenCalled()
  })
})

describe('preview guest scoping', () => {
  it('installs guards only for webviews in the preview partition', () => {
    const previewSession = {}
    const otherSession = {}
    const sessionApi = { fromPartition: vi.fn(() => previewSession) }
    let onWebContentsCreated: ((_event: unknown, contents: unknown) => void) | undefined

    const electronApp = {
      on: vi.fn((event: string, listener: (_event: unknown, contents: unknown) => void) => {
        if (event === 'web-contents-created') {
          onWebContentsCreated = listener
        }
      })
    }

    const previewGuest = mockGuest(previewSession)
    const unrelatedGuest = mockGuest(otherSession)

    installPreviewWebviewGuards(electronApp as never, sessionApi as never)
    onWebContentsCreated?.({}, unrelatedGuest)
    onWebContentsCreated?.({}, previewGuest)

    expect(unrelatedGuest.setWindowOpenHandler).not.toHaveBeenCalled()
    expect(unrelatedGuest.on).not.toHaveBeenCalled()
    expect(previewGuest.setWindowOpenHandler).toHaveBeenCalledTimes(1)
    expect(previewGuest.on).toHaveBeenCalledWith('before-input-event', expect.any(Function))
  })

  it('rejects explicit registration for a non-preview guest', () => {
    const previewSession = {}
    const unrelatedGuest = mockGuest({}, 42)

    expect(
      wirePreviewWebviewById(
        42,
        { fromId: vi.fn(() => unrelatedGuest) } as never,
        { fromPartition: vi.fn(() => previewSession) } as never
      )
    ).toBe(false)
    expect(unrelatedGuest.setWindowOpenHandler).not.toHaveBeenCalled()
    expect(unrelatedGuest.on).not.toHaveBeenCalled()
  })
})
