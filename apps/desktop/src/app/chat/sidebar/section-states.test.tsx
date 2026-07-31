import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SidebarCrossProfilePinsNotice } from './section-states'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      profiles: { showAllProfiles: 'Show all profiles' },
      sidebar: { shiftClickHint: 'Shift-click a chat to pin' }
    }
  })
}))

afterEach(cleanup)

describe('SidebarCrossProfilePinsNotice', () => {
  it('offers the all-profiles view when scoped pins are hidden', () => {
    const onShowAll = vi.fn()

    render(<SidebarCrossProfilePinsNotice count={2} onShowAll={onShowAll} />)

    fireEvent.click(screen.getByRole('button', { name: 'Show all profiles' }))

    expect(screen.getByText('+2')).toBeTruthy()
    expect(onShowAll).toHaveBeenCalledOnce()
  })
})
