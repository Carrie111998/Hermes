import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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

describe('MessageContextMenu', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders children inside a context menu trigger', () => {
    render(
      <MessageContextMenu messageId="msg-1">
        <div data-testid="child">Hello</div>
      </MessageContextMenu>
    )
    expect(screen.getByTestId('child')).toBeTruthy()
  })

  it('shows context menu items on right-click', async () => {
    render(
      <MessageContextMenu messageId="msg-1">
        <div data-testid="child">Selectable text here</div>
      </MessageContextMenu>
    )

    const child = screen.getByTestId('child')
    fireEvent.contextMenu(child)

    // Menu items appear in a portal
    expect(await screen.findByText('Add as context')).toBeTruthy()
    expect(screen.getByText('Paste as text')).toBeTruthy()
  })
})
