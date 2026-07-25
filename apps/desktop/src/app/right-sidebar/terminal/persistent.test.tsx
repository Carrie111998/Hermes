import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PersistentTerminal, TerminalSlot } from './persistent'

vi.mock('./terminals', () => ({ ensureTerminal: vi.fn() }))
vi.mock('./workspace', () => ({ TerminalWorkspace: () => <div /> }))

describe('PersistentTerminal', () => {
  let nextFrame: FrameRequestCallback | undefined

  beforeEach(() => {
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      nextFrame = callback

      return 1
    })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      bottom: 220,
      height: 200,
      left: 10,
      right: 310,
      top: 20,
      width: 300,
      x: 10,
      y: 20,
      toJSON: () => undefined
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('hides the fixed overlay when its kept-alive pane becomes inactive', () => {
    const view = render(
      <>
        <div>
          <TerminalSlot />
        </div>
        <PersistentTerminal onAddSelectionToChat={vi.fn()} />
      </>
    )

    const overlay = view.container.querySelector<HTMLElement>('[aria-hidden]')

    if (!overlay) {
      throw new Error('Persistent terminal overlay was not rendered')
    }

    expect(overlay.style.visibility).toBe('visible')

    view.rerender(
      <>
        <div data-pane-hidden>
          <TerminalSlot />
        </div>
        <PersistentTerminal onAddSelectionToChat={vi.fn()} />
      </>
    )
    act(() => nextFrame?.(0))

    expect(overlay.style.pointerEvents).toBe('none')
    expect(overlay.style.visibility).toBe('hidden')
  })
})
