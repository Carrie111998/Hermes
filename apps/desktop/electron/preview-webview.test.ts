import assert from 'node:assert/strict'

import { describe, expect, it, vi } from 'vitest'

import {
  previewWindowOpenDecision,
  shouldHandleEscapeAsPreviewBack,
  wirePreviewWebviewContents
} from './preview-webview'

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
