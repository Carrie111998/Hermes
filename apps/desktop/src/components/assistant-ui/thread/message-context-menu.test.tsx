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

  it('renders children as-is when no text is selected', () => {
    mockSelection('')

    render(
      <MessageContextMenu messageId="msg-1">
        <div data-testid="child">Hello</div>
      </MessageContextMenu>
    )
    expect(screen.getByTestId('child')).toBeTruthy()
    // No selection → no Radix ContextMenu wrapper → no menu items.
    expect(screen.queryByText('Add as context')).toBeNull()
  })

  it('shows context menu items on right-click when text is selected', async () => {
    mockSelection('Selectable text here')

    render(
      <MessageContextMenu messageId="msg-1">
        <div data-testid="child">Selectable text here</div>
      </MessageContextMenu>
    )

    // Drag-select completes → browser fires selectionchange → component
    // mounts the Radix wrapper. Wrap in act so the re-render flushes and
    // the trigger node is fresh before we right-click it.
    await act(async () => {
      document.dispatchEvent(new Event('selectionchange'))
    })

    const child = screen.getByTestId('child')
    openContextMenu(child)

    // Menu items appear in a portal
    expect(await screen.findByText('Add as context')).toBeTruthy()
    expect(screen.getByText('Paste as text')).toBeTruthy()
  })
})
