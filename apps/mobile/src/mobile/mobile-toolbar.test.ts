// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MobileToolbar } from './mobile-toolbar'

afterEach(cleanup)

describe('MobileToolbar', () => {
  it('keeps navigation in one horizontal bar and reveals all actions only from its overflow sheet', () => {
    const openSessions = vi.fn()
    const openActions = vi.fn()

    const { container } = render(
      createElement(MobileToolbar, {
        appActions: [{ id: 'settings', label: 'Settings', onSelect: vi.fn() }],
        contextActions: [{ id: 'refresh', label: 'Refresh preview', onSelect: vi.fn() }],
        menuOpen: false,
        onClose: vi.fn(),
        onOpenSessions: openSessions,
        onToggleMenu: openActions,
        sessionsOpen: false,
        workspacePanes: [{ id: 'files', title: 'Files' }]
      })
    )

    expect(container.querySelectorAll('.mobile-top-toolbar')).toHaveLength(1)
    expect(container.querySelectorAll('.mobile-top-toolbar > button')).toHaveLength(2)
    expect(screen.queryByRole('dialog', { name: 'Desktop actions' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Show sidebar' }))
    fireEvent.click(screen.getByRole('button', { name: 'Open desktop actions' }))
    expect(openSessions).toHaveBeenCalledOnce()
    expect(openActions).toHaveBeenCalledOnce()
  })

  it('makes workspace, interface, and contextual desktop actions reachable from the one sheet', () => {
    const onFiles = vi.fn()
    const onSettings = vi.fn()
    const onRefresh = vi.fn()
    const onClose = vi.fn()

    render(
      createElement(MobileToolbar, {
        appActions: [{ id: 'settings', label: 'Settings', onSelect: onSettings }],
        contextActions: [{ id: 'refresh', label: 'Refresh preview', onSelect: onRefresh }],
        menuOpen: true,
        onClose,
        onOpenSessions: vi.fn(),
        onToggleMenu: vi.fn(),
        onWorkspacePane: onFiles,
        sessionsOpen: false,
        workspacePanes: [{ id: 'files', title: 'Files' }]
      })
    )

    const dialog = screen.getByRole('dialog', { name: 'Desktop actions' })
    expect(dialog.getAttribute('data-mobile-toolbar-origin')).toBe('top-right')

    fireEvent.click(screen.getByRole('button', { name: 'Files' }))
    expect(onFiles).toHaveBeenCalledWith('files')

    fireEvent.click(screen.getByRole('button', { name: 'Settings' }))
    expect(onSettings).toHaveBeenCalledOnce()
    expect(onClose).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh preview' }))
    expect(onRefresh).toHaveBeenCalledOnce()
    expect(onClose).toHaveBeenCalledTimes(3)
  })

  it('moves focus into the modal sheet and returns it to the overflow trigger when dismissed', () => {
    const props = {
      appActions: [],
      contextActions: [],
      onClose: vi.fn(),
      onOpenSessions: vi.fn(),
      onToggleMenu: vi.fn(),
      sessionsOpen: false,
      workspacePanes: []
    }
    const { rerender } = render(createElement(MobileToolbar, { ...props, menuOpen: false }))
    const overflow = screen.getByRole('button', { name: 'Open desktop actions' })

    rerender(createElement(MobileToolbar, { ...props, menuOpen: true }))
    expect(document.activeElement).toBe(
      within(screen.getByRole('dialog', { name: 'Desktop actions' })).getByRole('button', {
        name: 'Close desktop actions'
      })
    )

    rerender(createElement(MobileToolbar, { ...props, menuOpen: false }))
    expect(document.activeElement).toBe(overflow)
  })

  it('lets a hardware keyboard dismiss the same sheet with Escape', () => {
    const onClose = vi.fn()
    render(
      createElement(MobileToolbar, {
        appActions: [],
        contextActions: [],
        menuOpen: true,
        onClose,
        onOpenSessions: vi.fn(),
        onToggleMenu: vi.fn(),
        sessionsOpen: false,
        workspacePanes: []
      })
    )

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })
})
