import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { MessageContextMenu } from '@/components/assistant-ui/thread/message-context-menu'

// Radix ContextMenu uses PointerEvent; jsdom doesn't fire it by default.
// fireEvent.contextMenu triggers the right-click that opens the menu.
// The menu content renders in a portal, so we query by role.

vi.mock('@/store/composer', () => ({
  addComposerTextAttachment: vi.fn(),
}))

vi.mock('@/app/chat/composer/focus', () => ({
  requestComposerInsert: vi.fn(),
}))

vi.mock('@/components/ui/copy-button', () => ({
  writeClipboardText: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: {
        addAsContext: 'Add as context',
        copy: 'Copy',
        pasteAsText: 'Paste as text',
        selectAll: 'Select All',
      },
    },
  }),
}))

beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
})

function mockSelection(text: string): void {
  // A collapsed selection (or no selection) means the native context menu
  // should win — the custom menu only mounts when text is highlighted.
  const selection = {
    isCollapsed: text.length === 0,
    toString: () => text,
  }
  vi.spyOn(window, 'getSelection').mockReturnValue(selection as Selection)
}

/** Radix opens a ContextMenu on contextmenu after a pointerdown positions it. */
function openContextMenu(target: HTMLElement) {
  fireEvent.pointerDown(target, { button: 2, pointerType: 'mouse' })
  fireEvent.contextMenu(target, { button: 2 })
}

describe('MessageContextMenu', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders children and keeps the menu closed when no text is selected', async () => {
    mockSelection('')

    render(
      <MessageContextMenu messageId="msg-1">
        <div data-testid="child">Hello</div>
      </MessageContextMenu>
    )

    // Children always render — the wrapper stays mounted so the DOM tree
    // is stable across selection changes (no mid-drag remounts).
    expect(screen.getByTestId('child')).toBeTruthy()

    // No selection → trigger is disabled → right-click shows nothing from us.
    const child = screen.getByTestId('child')
    openContextMenu(child)

    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.queryByText('Add as context')).toBeNull()
  })

  it('keeps Copy and Select All alongside the context items when text is selected', async () => {
    mockSelection('Selectable text here')

    render(
      <MessageContextMenu messageId="msg-1">
        <div data-testid="child">Selectable text here</div>
      </MessageContextMenu>
    )

    // Drag-select completes → browser fires selectionchange → the trigger
    // enables. Wrap in act so the re-render flushes before we right-click.
    await act(async () => {
      document.dispatchEvent(new Event('selectionchange'))
    })

    const child = screen.getByTestId('child')
    openContextMenu(child)

    // Menu items appear in a portal — the standard actions stay, our two
    // additions follow below the separator.
    expect(await screen.findByText('Copy')).toBeTruthy()
    expect(screen.getByText('Select All')).toBeTruthy()
    expect(screen.getByText('Add as context')).toBeTruthy()
    expect(screen.getByText('Paste as text')).toBeTruthy()
  })
})
