import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useHudComposerDrag } from './composer-drag'

const LONG_PRESS_MS = 140

describe('useHudComposerDrag', () => {
  const moveBy = vi.fn()

  beforeEach(() => {
    vi.useFakeTimers()
    moveBy.mockReset()
    Object.defineProperty(window, 'outerWidth', { configurable: true, value: 620 })
    Object.defineProperty(window, 'outerHeight', { configurable: true, value: 320 })
    window.hermesDesktop = {
      hud: { moveBy }
    } as typeof window.hermesDesktop
  })

  afterEach(() => {
    vi.useRealTimers()
    delete window.hermesDesktop
  })

  it('pins the window size from when the drag arms on every moveBy', () => {
    const target = document.createElement('div')
    target.setPointerCapture = vi.fn()
    target.hasPointerCapture = vi.fn(() => true)
    target.releasePointerCapture = vi.fn()
    document.body.append(target)

    const { result } = renderHook(() => useHudComposerDrag(true))

    act(() => {
      result.current.onPointerDown({
        button: 0,
        currentTarget: target,
        pointerId: 1,
        preventDefault: vi.fn(),
        screenX: 100,
        screenY: 200
      } as never)
    })

    act(() => {
      vi.advanceTimersByTime(LONG_PRESS_MS)
    })

    act(() => {
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, screenX: 110, screenY: 210 }))
    })

    expect(moveBy).toHaveBeenCalledWith({ x: 10, y: 10, width: 620, height: 320 })

    Object.defineProperty(window, 'outerWidth', { configurable: true, value: 900 })
    Object.defineProperty(window, 'outerHeight', { configurable: true, value: 500 })

    act(() => {
      window.dispatchEvent(new PointerEvent('pointermove', { pointerId: 1, screenX: 115, screenY: 215 }))
    })

    expect(moveBy).toHaveBeenLastCalledWith({ x: 5, y: 5, width: 620, height: 320 })
  })
})
