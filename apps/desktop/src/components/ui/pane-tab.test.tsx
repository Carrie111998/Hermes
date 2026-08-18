import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PaneTab, PaneTabLabel } from './pane-tab'

afterEach(cleanup)

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

describe('PaneTab hover close button', () => {
  it('renders a close button when onClose is set on a horizontal tab', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.getByRole('button', { name: 'Close' })).toBeTruthy()
  })

  it('reveals the close button for keyboard focus as well as pointer hover', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.getByRole('button', { name: 'Close' }).className).toContain('focus-visible:opacity-100')
  })

  it('retains the dirty marker on a closeable tab', () => {
    const { container } = render(
      <PaneTab dirty onClose={vi.fn()}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(container.querySelector('[data-pane-tab-dirty]')).not.toBeNull()
  })

  it('clicking the close button calls onClose and stops propagation', () => {
    const onClose = vi.fn()
    const onTabPointerDown = vi.fn()
    render(
      <PaneTab onClose={onClose} onPointerDown={onTabPointerDown}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    const closeBtn = screen.getByRole('button', { name: 'Close' })
    fireEvent.pointerDown(closeBtn)
    fireEvent.click(closeBtn)
    expect(onClose).toHaveBeenCalledTimes(1)
    // The tab's own pointerdown handler must NOT fire — the X is a leaf action.
    expect(onTabPointerDown).not.toHaveBeenCalled()
  })

  it('keeps the established meta-click close gesture over the close button', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    const closeBtn = screen.getByRole('button', { name: 'Close' })
    fireEvent.pointerDown(closeBtn, { button: 0, metaKey: true })
    fireEvent.click(closeBtn, { button: 0, metaKey: true })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('is focusable and closes from a keyboard-style activation click', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose}>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    const closeBtn = screen.getByRole('button', { name: 'Close' })
    closeBtn.focus()
    expect(closeBtn.ownerDocument.activeElement).toBe(closeBtn)

    fireEvent.click(closeBtn, { detail: 0 })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('does not render a close button on vertical tabs', () => {
    const onClose = vi.fn()
    render(
      <PaneTab onClose={onClose} vertical>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
  })

  it('does not render a close button without onClose', () => {
    render(
      <PaneTab>
        <PaneTabLabel>tab</PaneTabLabel>
      </PaneTab>
    )

    expect(screen.queryByRole('button', { name: 'Close' })).toBeNull()
  })
})
