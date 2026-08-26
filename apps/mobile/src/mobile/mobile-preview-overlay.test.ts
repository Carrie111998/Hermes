// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { previewPane } = vi.hoisted(() => ({ previewPane: vi.fn() }))

vi.mock('@/app/chat/right-rail/preview-pane', () => ({
  PreviewPane: (props: unknown) => {
    previewPane(props)
    return null
  },
}))

import type { PreviewTab } from '@/store/preview'

import { MobilePreviewOverlay } from './mobile-preview-overlay'

const browserTab = (id: `url:${string}`, url: string): PreviewTab => ({
  id,
  target: { kind: 'url' as const, label: 'Browser', source: url, url },
})

afterEach(() => {
  cleanup()
  previewPane.mockReset()
})

describe('MobilePreviewOverlay', () => {
  it('keeps browser tabs in one full-screen surface where users can switch tabs, close one tab, or close the surface without losing tabs', () => {
    const onClose = vi.fn()
    const onCloseTab = vi.fn()
    const onSelectTab = vi.fn()
    const tabs = [browserTab('url:one', 'https://one.example.test'), browserTab('url:two', 'https://two.example.test')]

    render(
      createElement(MobilePreviewOverlay, {
        activeTabId: 'url:one',
        onClose,
        onCloseTab,
        onNavigate: vi.fn(),
        onNewBrowserTab: vi.fn(),
        onOpenExternal: vi.fn(),
        onSelectTab,
        open: true,
        tabs,
      }),
    )

    const dialog = screen.getByRole('dialog', { name: 'Preview' })
    expect(within(dialog).getByTitle('Browser')).toBeTruthy()

    fireEvent.click(within(dialog).getAllByRole('tab', { name: 'Browser' })[0])
    expect(onSelectTab).toHaveBeenCalledWith('url:one')

    fireEvent.click(within(dialog).getAllByRole('button', { name: 'Close Browser' })[0])
    expect(onCloseTab).toHaveBeenCalledWith('url:one')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Close preview' }))
    expect(onClose).toHaveBeenCalledOnce()
  })

  it('loads only HTTPS addresses inline and keeps a deliberate system-browser escape path', () => {
    const onNavigate = vi.fn()
    const onOpenExternal = vi.fn()

    render(
      createElement(MobilePreviewOverlay, {
        activeTabId: 'url:one',
        onClose: vi.fn(),
        onCloseTab: vi.fn(),
        onNavigate,
        onNewBrowserTab: vi.fn(),
        onOpenExternal,
        onSelectTab: vi.fn(),
        open: true,
        tabs: [browserTab('url:one', 'https://one.example.test')],
      }),
    )

    const dialog = screen.getByRole('dialog', { name: 'Preview' })
    expect(within(dialog).getByText('Open')).toBeTruthy()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Open in system browser' }))
    expect(onOpenExternal).toHaveBeenCalledWith('https://one.example.test')

    const address = within(dialog).getByRole('textbox', { name: 'Address' })
    fireEvent.change(address, { target: { value: 'http://insecure.example.test' } })
    fireEvent.keyDown(address, { key: 'Enter' })
    expect(onNavigate).not.toHaveBeenCalled()

    fireEvent.change(address, { target: { value: 'https://safe.example.test/path' } })
    fireEvent.keyDown(address, { key: 'Enter' })
    expect(onNavigate).toHaveBeenCalledWith('https://safe.example.test/path')
  })

  it('keeps file, PDF, Markdown, image, and artifact targets on the shared Desktop preview path', () => {
    const fileTab: PreviewTab = {
      id: 'file:/workspace/README.md',
      target: {
        kind: 'file',
        label: 'README.md',
        language: 'markdown',
        path: '/workspace/README.md',
        previewKind: 'text',
        source: '/workspace/README.md',
        url: 'file:///workspace/README.md',
      },
    }

    render(
      createElement(MobilePreviewOverlay, {
        activeTabId: fileTab.id,
        onClose: vi.fn(),
        onCloseTab: vi.fn(),
        onNavigate: vi.fn(),
        onNewBrowserTab: vi.fn(),
        onOpenExternal: vi.fn(),
        onSelectTab: vi.fn(),
        open: true,
        tabs: [fileTab],
      }),
    )

    expect(previewPane).toHaveBeenCalledWith(expect.objectContaining({ embedded: true, tabId: fileTab.id, target: fileTab.target }))
  })

  it('dismisses the full-screen preview with Escape without closing its tabs', () => {
    const onClose = vi.fn()

    render(
      createElement(MobilePreviewOverlay, {
        activeTabId: 'url:one',
        onClose,
        onCloseTab: vi.fn(),
        onNavigate: vi.fn(),
        onNewBrowserTab: vi.fn(),
        onOpenExternal: vi.fn(),
        onSelectTab: vi.fn(),
        open: true,
        tabs: [browserTab('url:one', 'https://one.example.test')],
      }),
    )

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledOnce()
  })
})
