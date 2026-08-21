// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $overlayPeek, resetOverlayPeek } from '@/store/overlay-peek'
import { $sessionListDensity } from '@/store/session-list-density'

import { SessionDensitySetting } from './session-density-setting'

const haptic = vi.fn()

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      settings: {
        appearance: {
          sessionDensityCompact: 'Compact',
          sessionDensityComfortable: 'Comfortable',
          sessionDensityDesc: 'Choose how much context appears beneath session titles in the sidebar.',
          sessionDensityDetailed: 'Detailed',
          sessionDensityPreview: 'Hold to preview your current session list',
          sessionDensityTitle: 'Session List Density'
        }
      }
    }
  })
}))

vi.mock('@/lib/haptics', () => ({
  triggerHaptic: (...args: unknown[]) => haptic(...args)
}))

const active = () => window.document.documentElement.hasAttribute('data-hermes-overlay-peek')

describe('SessionDensitySetting', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    window.localStorage.clear()
    $sessionListDensity.set('compact')
    resetOverlayPeek()
  })

  afterEach(() => {
    cleanup()
    resetOverlayPeek()
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('applies a density choice and briefly reveals the real sidebar', () => {
    render(<SessionDensitySetting />)

    fireEvent.click(screen.getByRole('button', { name: 'Comfortable' }))

    expect($sessionListDensity.get()).toBe('comfortable')
    expect(haptic).toHaveBeenCalledWith('selection')
    expect(active()).toBe(true)

    act(() => vi.runAllTimers())
    expect(active()).toBe(false)
  })

  it('keeps the overlay ghosted for the duration of a pointer hold', () => {
    render(<SessionDensitySetting />)
    const preview = screen.getByRole('button', { name: 'Hold to preview your current session list' })

    fireEvent.pointerDown(preview, { button: 0, pointerId: 1 })
    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    act(() => vi.advanceTimersByTime(300))
    fireEvent.pointerUp(preview, { button: 0, pointerId: 1 })
    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)
  })

  it('turns a quick pointer tap into a bounded preview', () => {
    render(<SessionDensitySetting />)
    const preview = screen.getByRole('button', { name: 'Hold to preview your current session list' })

    fireEvent.pointerDown(preview, { button: 0, pointerId: 1 })
    fireEvent.pointerUp(preview, { button: 0, pointerId: 1 })

    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    act(() => vi.runAllTimers())
    expect(active()).toBe(false)
  })

  it('supports a keyboard hold without counting key repeat twice', () => {
    render(<SessionDensitySetting />)
    const preview = screen.getByRole('button', { name: 'Hold to preview your current session list' })

    fireEvent.keyDown(preview, { key: ' ', repeat: false })
    fireEvent.keyDown(preview, { key: ' ', repeat: true })

    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    // Enter may emit click on keydown; Space may emit it after keyup. Neither
    // may turn a long hold into a second timed pulse.
    fireEvent.click(preview, { detail: 0 })
    act(() => vi.advanceTimersByTime(300))
    fireEvent.keyUp(preview, { key: ' ' })
    fireEvent.click(preview, { detail: 0 })
    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)
  })

  it('turns a quick keyboard tap into one bounded preview', () => {
    render(<SessionDensitySetting />)
    const preview = screen.getByRole('button', { name: 'Hold to preview your current session list' })

    fireEvent.keyDown(preview, { key: 'Enter', repeat: false })
    fireEvent.click(preview, { detail: 0 })
    fireEvent.keyUp(preview, { key: 'Enter' })
    fireEvent.click(preview, { detail: 0 })

    expect($overlayPeek.get()).toBe(1)
    expect(active()).toBe(true)

    act(() => vi.runAllTimers())
    expect(active()).toBe(false)
  })

  it('cancels keyboard state on blur so the next hold still works', () => {
    render(<SessionDensitySetting />)
    const preview = screen.getByRole('button', { name: 'Hold to preview your current session list' })

    fireEvent.keyDown(preview, { key: ' ', repeat: false })
    expect(active()).toBe(true)

    fireEvent.blur(preview)
    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)

    fireEvent.keyDown(preview, { key: ' ', repeat: false })
    act(() => vi.advanceTimersByTime(300))
    fireEvent.keyUp(preview, { key: ' ' })

    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)
  })

  it('offers a bounded preview for click and assistive activation', () => {
    render(<SessionDensitySetting />)
    const preview = screen.getByRole('button', { name: 'Hold to preview your current session list' })

    fireEvent.click(preview)
    expect(active()).toBe(true)

    act(() => vi.runAllTimers())
    expect(active()).toBe(false)
  })

  it('fails closed when the setting unmounts during a hold', () => {
    const view = render(<SessionDensitySetting />)
    const preview = screen.getByRole('button', { name: 'Hold to preview your current session list' })

    fireEvent.pointerDown(preview, { button: 0, pointerId: 1 })
    expect(active()).toBe(true)

    view.unmount()
    expect($overlayPeek.get()).toBe(0)
    expect(active()).toBe(false)
  })
})
