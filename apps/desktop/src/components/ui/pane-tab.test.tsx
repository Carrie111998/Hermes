import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setRuntimeI18nLocale } from '@/i18n/runtime'

import { PaneTab, PaneTabLabel } from './pane-tab'

afterEach(() => {
  cleanup()
  setRuntimeI18nLocale('en')
})

describe('PaneTab close gestures', () => {
  it('middle-click closes — pointer events only, no auxclick', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    const tab = screen.getByText('tab')
    fireEvent.pointerDown(tab, { button: 1 })
    fireEvent.pointerUp(tab, { button: 1 })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('⌘-click (metaKey + button 0) closes — the Mac middle-click equivalent', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    fireEvent.pointerDown(screen.getByText('tab'), { button: 0, metaKey: true })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('⌘-click preempts the shell drag/activate pointerdown handler', () => {
    const onClose = vi.fn()
    const onPointerDown = vi.fn()
    render(
      <PaneTab onClose={onClose} onPointerDown={onPointerDown}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    fireEvent.pointerDown(screen.getByText('tab'), { button: 0, metaKey: true })
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onPointerDown).not.toHaveBeenCalled()
  })

  it('⌘-click swallows the follow-up activation click (capture phase)', () => {
    const onClose = vi.fn()
    const onActivate = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel as="button" onClick={onActivate}>
          tab
        </PaneTabLabel>
      </PaneTab>
    )

    fireEvent.click(screen.getByText('tab'), { button: 0, metaKey: true })
    expect(onActivate).not.toHaveBeenCalled()
  })

  it('plain left-click neither closes nor blocks activation', () => {
    const onClose = vi.fn()
    const onActivate = vi.fn()
    const onPointerDown = vi.fn()
    render(
      <PaneTab onClose={onClose} onPointerDown={onPointerDown}>
        <PaneTabLabel as="button" onClick={onActivate}>
          tab
        </PaneTabLabel>
      </PaneTab>
    )

    fireEvent.pointerDown(screen.getByText('tab'), { button: 0 })
    fireEvent.click(screen.getByText('tab'), { button: 0 })
    expect(onClose).not.toHaveBeenCalled()
    expect(onPointerDown).toHaveBeenCalledTimes(1)
    expect(onActivate).toHaveBeenCalledTimes(1)
  })

  it('does nothing without an onClose (uncloseable workspace tab)', () => {
    const onPointerDown = vi.fn()
    render(
      <PaneTab onPointerDown={onPointerDown}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    fireEvent.pointerDown(screen.getByText('tab'), { button: 0, metaKey: true })
    expect(onPointerDown).toHaveBeenCalledTimes(1)
  })
})

describe('PaneTab close button', () => {
  it('renders an accessible close button for a closeable horizontal tab', () => {
    render(
      <PaneTab onClose={vi.fn()}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.getByRole('button', { name: 'Close tab' })).toBeTruthy()
  })

  it('uses the active locale for the close button label', () => {
    setRuntimeI18nLocale('zh')
    render(
      <PaneTab onClose={vi.fn()}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.getByRole('button', { name: '关闭标签' })).toBeTruthy()
  })

  it('reveals the close button when keyboard focus reaches it', () => {
    render(
      <PaneTab onClose={vi.fn()}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.getByRole('button', { name: 'Close tab' }).className).toContain('focus-visible:opacity-100')
  })

  it('keeps the dirty indicator until hover reveals the close glyph', () => {
    const { container } = render(
      <PaneTab dirty onClose={vi.fn()}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(container.querySelector('[data-slot="pane-tab-dirty-indicator"]')).toBeTruthy()
  })

  it('clicking the close button calls onClose and stops propagation', () => {
    const onClose = vi.fn()
    const onTabPointerDown = vi.fn()
    render(
      <PaneTab onClose={onClose} onPointerDown={onTabPointerDown}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    const closeBtn = screen.getByRole('button', { name: 'Close tab' })
    fireEvent.pointerDown(closeBtn)
    fireEvent.click(closeBtn)
    expect(onClose).toHaveBeenCalledTimes(1)
    // The tab's own pointerdown handler must NOT fire — the X is a leaf action.
    expect(onTabPointerDown).not.toHaveBeenCalled()
  })

  it('does not render a close button on vertical tabs', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose} vertical>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.queryByRole('button', { name: 'Close tab' })).toBeNull()
  })

  it('does not render a close button without onClose', () => {
    render(
      <PaneTab>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.queryByRole('button', { name: 'Close tab' })).toBeNull()
  })
})
