import { act, renderHook } from '@testing-library/react'
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useHudComposerDrag } from './composer-drag'

const { triggerHaptic } = vi.hoisted(() => ({ triggerHaptic: vi.fn() }))
const moveBy = vi.fn()

vi.mock('@/lib/haptics', () => ({ triggerHaptic }))

function pointerEvent(
  currentTarget: HTMLElement,
  target: HTMLElement,
  screenX: number,
  screenY: number
): ReactPointerEvent<HTMLElement> {
  return {
    button: 0,
    currentTarget,
    pointerId: 7,
    preventDefault: vi.fn(),
    screenX,
    screenY,
    target
  } as unknown as ReactPointerEvent<HTMLElement>
}

function keyboardEvent(key: string, shiftKey = false): ReactKeyboardEvent<HTMLElement> {
  return {
    key,
    preventDefault: vi.fn(),
    shiftKey
  } as unknown as ReactKeyboardEvent<HTMLElement>
}

function dispatchPointer(type: 'pointermove' | 'pointerup' | 'pointercancel', screenX: number, screenY: number) {
  const event = new MouseEvent(type, { bubbles: true })

  Object.defineProperties(event, {
    pointerId: { value: 7 },
    screenX: { value: screenX },
    screenY: { value: screenY }
  })

  window.dispatchEvent(event)
}

describe('useHudComposerDrag', () => {
  let composer: HTMLElement
  let grip: HTMLElement
  const setPointerCapture = vi.fn<(pointerId: number) => void>()
  const releasePointerCapture = vi.fn<(pointerId: number) => void>()

  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { hud: { moveBy } } })

    composer = globalThis.document.createElement('div')
    grip = globalThis.document.createElement('span')
    grip.setAttribute('data-hud-drag-grip', '')
    composer.append(grip)
    globalThis.document.body.append(composer)

    composer.setPointerCapture = setPointerCapture
    composer.releasePointerCapture = releasePointerCapture
    composer.hasPointerCapture = vi.fn(() => true)
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.document.body.innerHTML = ''
  })

  it('moves immediately from the visible grip and suppresses its follow-up click', () => {
    const { result } = renderHook(() => useHudComposerDrag(true))
    const click = vi.fn()
    grip.addEventListener('click', click)

    act(() => result.current.onPointerDown(pointerEvent(composer, grip, 100, 200)))
    act(() => dispatchPointer('pointermove', 107, 194))
    act(() => dispatchPointer('pointerup', 107, 194))
    grip.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    expect(setPointerCapture).toHaveBeenCalledWith(7)
    expect(moveBy).toHaveBeenCalledWith({ x: 7, y: -6 })
    expect(click).not.toHaveBeenCalled()
    expect(releasePointerCapture).toHaveBeenCalledWith(7)
  })

  it('moves the HUD with arrow keys and accelerates with Shift', () => {
    const { result } = renderHook(() => useHudComposerDrag(true))
    const left = keyboardEvent('ArrowLeft')
    const downFast = keyboardEvent('ArrowDown', true)
    const enter = keyboardEvent('Enter')

    act(() => result.current.onGripKeyDown(left))
    act(() => result.current.onGripKeyDown(downFast))
    act(() => result.current.onGripKeyDown(enter))

    expect(moveBy).toHaveBeenNthCalledWith(1, { x: -12, y: 0 })
    expect(moveBy).toHaveBeenNthCalledWith(2, { x: 0, y: 48 })
    expect(moveBy).toHaveBeenCalledTimes(2)
    expect(left.preventDefault).toHaveBeenCalledOnce()
    expect(downFast.preventDefault).toHaveBeenCalledOnce()
    expect(enter.preventDefault).not.toHaveBeenCalled()
  })

  it('keeps the long-press composer fallback', () => {
    const { result } = renderHook(() => useHudComposerDrag(true))

    act(() => result.current.onPointerDown(pointerEvent(composer, composer, 100, 200)))
    act(() => vi.advanceTimersByTime(140))
    act(() => dispatchPointer('pointermove', 96, 211))

    expect(setPointerCapture).toHaveBeenCalledWith(7)
    expect(moveBy).toHaveBeenCalledWith({ x: -4, y: 11 })
  })

  it('cancels an unarmed composer press that becomes a normal drag', () => {
    const { result } = renderHook(() => useHudComposerDrag(true))

    act(() => result.current.onPointerDown(pointerEvent(composer, composer, 100, 200)))
    act(() => dispatchPointer('pointermove', 109, 200))
    act(() => vi.advanceTimersByTime(140))

    expect(setPointerCapture).not.toHaveBeenCalled()
    expect(moveBy).not.toHaveBeenCalled()
    expect(triggerHaptic).not.toHaveBeenCalled()
  })

  it('keeps screen-coordinate reverse and diagonal deltas after a grip drag arms', () => {
    const { result } = renderHook(() => useHudComposerDrag(true))

    act(() => result.current.onPointerDown(pointerEvent(composer, grip, 100, 200)))
    act(() => dispatchPointer('pointermove', 115, 185))
    act(() => dispatchPointer('pointermove', 108, 191))

    expect(moveBy).toHaveBeenNthCalledWith(1, { x: 15, y: -15 })
    expect(moveBy).toHaveBeenNthCalledWith(2, { x: -7, y: 6 })
  })
})
