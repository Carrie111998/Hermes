import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { OverlayView } from './overlay-view'

describe('OverlayView', () => {
  afterEach(cleanup)

  it('renders card with data-glass-raised and edgeBadge with data-glass-opaque (#98484)', () => {
    const onClose = vi.fn()
    const { container } = render(
      <OverlayView
        closeLabel="Close"
        edgeBadge={<span data-testid="search-pill">Search Settings</span>}
        onClose={onClose}
      >
        <div data-testid="content">Settings Content</div>
      </OverlayView>
    )

    // The card has data-glass-raised
    const raisedCard = container.querySelector('[data-glass-raised]')
    expect(raisedCard).not.toBeNull()
    expect(screen.getByTestId('content')).toBeDefined()

    // The edgeBadge wrapper has data-glass-opaque
    const opaqueBadgeWrapper = container.querySelector('[data-glass-opaque]')
    expect(opaqueBadgeWrapper).not.toBeNull()
    expect(opaqueBadgeWrapper?.contains(screen.getByTestId('search-pill'))).toBe(true)
  })
})
