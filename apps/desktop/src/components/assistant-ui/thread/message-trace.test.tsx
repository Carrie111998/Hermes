import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MessageTrace } from './message-trace'
import type { TracePosition } from './message-trace'

afterEach(() => {
  cleanup()
})

describe('MessageTrace', () => {
  it('renders a slider with accessible label when untraced', () => {
    const onTrace = vi.fn()

    render(<MessageTrace onTrace={onTrace} />)
    const slider = screen.getByRole('slider')

    expect(slider).toBeTruthy()
    expect(slider.getAttribute('aria-label')).toContain('click to score')
    expect(slider.getAttribute('tabindex')).toBe('0')
  })

  it('reports score when clicked', () => {
    const onTrace = vi.fn()

    render(<MessageTrace onTrace={onTrace} />)
    const slider = screen.getByRole('slider')

    // Mock getBoundingClientRect: 200px tall, click at y=30 (near top)
    vi.spyOn(slider, 'getBoundingClientRect').mockReturnValue({
      height: 200,
      top: 0,
      x: 0,
      y: 0,
      width: 20,
      bottom: 200,
      left: 0,
      right: 20,
      toJSON: () => null
    })

    fireEvent.click(slider, { clientY: 30 })

    expect(onTrace).toHaveBeenCalledTimes(1)
    const position: TracePosition = onTrace.mock.calls[0][0]

    expect(position.y).toBeCloseTo(0.15, 1) // 30/200 = 0.15
    expect(position.score).toBe(85) // (1 - 0.15) * 100 = 85 — top = high score
  })

  it('click at bottom gives low score', () => {
    const onTrace = vi.fn()

    render(<MessageTrace onTrace={onTrace} />)
    const slider = screen.getByRole('slider')

    vi.spyOn(slider, 'getBoundingClientRect').mockReturnValue({
      height: 200,
      top: 0,
      x: 0,
      y: 0,
      width: 20,
      bottom: 200,
      left: 0,
      right: 20,
      toJSON: () => null
    })

    fireEvent.click(slider, { clientY: 170 })

    expect(onTrace).toHaveBeenCalledTimes(1)
    const position: TracePosition = onTrace.mock.calls[0][0]

    expect(position.score).toBe(15) // (1 - 0.85) * 100 = 15
  })

  it('click at middle gives ~50 score', () => {
    const onTrace = vi.fn()

    render(<MessageTrace onTrace={onTrace} />)
    const slider = screen.getByRole('slider')

    vi.spyOn(slider, 'getBoundingClientRect').mockReturnValue({
      height: 200,
      top: 0,
      x: 0,
      y: 0,
      width: 20,
      bottom: 200,
      left: 0,
      right: 20,
      toJSON: () => null
    })

    fireEvent.click(slider, { clientY: 100 })

    expect(onTrace).toHaveBeenCalledTimes(1)
    const position: TracePosition = onTrace.mock.calls[0][0]

    expect(position.score).toBe(50)
  })

  it('shows the trace score and glow when traced', () => {
    const onTrace = vi.fn()

    render(
      <MessageTrace
        onTrace={onTrace}
        trace={{ score: 72 }}
      />
    )
    const slider = screen.getByRole('slider')

    expect(slider.getAttribute('aria-label')).toContain('score 72')
    expect(slider.getAttribute('title')).toBe('Score: 72/100')

    // Left border glow should be present
    const glow = document.querySelector('.animate-settle')

    expect(glow).toBeTruthy()
  })

  it('no glow when not traced', () => {
    const onTrace = vi.fn()

    render(<MessageTrace onTrace={onTrace} />)
    const glow = document.querySelector('.animate-settle')

    expect(glow).toBeFalsy()
  })

  it('keyboard: Enter sets score 50', () => {
    const onTrace = vi.fn()

    render(<MessageTrace onTrace={onTrace} />)
    const slider = screen.getByRole('slider')

    fireEvent.keyDown(slider, { key: 'Enter' })

    expect(onTrace).toHaveBeenCalledTimes(1)
    expect(onTrace.mock.calls[0][0]).toEqual({ y: 0.5, score: 50 })
  })

  it('keyboard: Space sets score 50', () => {
    const onTrace = vi.fn()

    render(<MessageTrace onTrace={onTrace} />)
    const slider = screen.getByRole('slider')

    fireEvent.keyDown(slider, { key: ' ' })

    expect(onTrace).toHaveBeenCalledTimes(1)
    expect(onTrace.mock.calls[0][0]).toEqual({ y: 0.5, score: 50 })
  })

  it('clips out-of-range click positions', () => {
    const onTrace = vi.fn()

    render(<MessageTrace onTrace={onTrace} />)
    const slider = screen.getByRole('slider')

    vi.spyOn(slider, 'getBoundingClientRect').mockReturnValue({
      height: 200,
      top: 0,
      x: 0,
      y: 0,
      width: 20,
      bottom: 200,
      left: 0,
      right: 20,
      toJSON: () => null
    })

    // Click above the element
    fireEvent.click(slider, { clientY: -50 })

    expect(onTrace).toHaveBeenCalledTimes(1)
    // Score should be clamped: y=0 → score=100
    expect(onTrace.mock.calls[0][0].score).toBe(100)
  })
})
