import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { useZoomPan } from './use-zoom-pan'

describe('useZoomPan fit', () => {
  it('scales content up to fill the stage', () => {
    const { result } = renderHook(() => useZoomPan())

    act(() => result.current.fit(2.5))

    expect(result.current.scale).toBe(2.5)
  })

  it('never fits below 1 — content already fills the stage stays put', () => {
    const { result } = renderHook(() => useZoomPan())

    act(() => result.current.fit(0.5))

    expect(result.current.scale).toBe(1)
  })

  it('clamps fit to the max zoom', () => {
    const { result } = renderHook(() => useZoomPan())

    act(() => result.current.fit(100))

    expect(result.current.scale).toBe(8)
  })

  it('fit centers the content (no pan offset)', () => {
    const { result } = renderHook(() => useZoomPan())

    act(() => result.current.fit(1.8))

    expect(result.current.style.transform).toBe('translate(0px, 0px) scale(1.8)')
  })
})
