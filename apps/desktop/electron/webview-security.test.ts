import type { WebContents } from 'electron'
import { describe, expect, it, vi } from 'vitest'

import { guardUntrustedWebviewWindowOpen } from './webview-security'

describe('guardUntrustedWebviewWindowOpen', () => {
  it('installs a deny-only window-open handler on webview guests', () => {
    const setWindowOpenHandler = vi.fn()

    const contents = {
      getType: () => 'webview',
      setWindowOpenHandler
    }

    expect(guardUntrustedWebviewWindowOpen(contents as unknown as WebContents)).toBe(true)
    expect(setWindowOpenHandler).toHaveBeenCalledOnce()

    const handler = setWindowOpenHandler.mock.calls[0]?.[0]

    expect(handler?.({ url: 'https://attacker.invalid/popup' })).toEqual({ action: 'deny' })
  })

  it('does not alter trusted non-webview contents', () => {
    const setWindowOpenHandler = vi.fn()

    expect(
      guardUntrustedWebviewWindowOpen(
        {
          getType: () => 'window',
          setWindowOpenHandler
        } as unknown as WebContents
      )
    ).toBe(false)
    expect(setWindowOpenHandler).not.toHaveBeenCalled()
  })
})
