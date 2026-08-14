import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $sidebarGrouping, resetSidebarView, setSidebarGrouping } from '@/store/layout'
import { $savedSidebarViews, saveCurrentSidebarView } from '@/store/sidebar-views'

import { SidebarSavedViewsQuickAccess } from './sidebar-views-quick-access'

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)

beforeEach(() => {
  resetSidebarView()
  $savedSidebarViews.set({ version: 1, views: [] })
})

afterEach(cleanup)

describe('SidebarSavedViewsQuickAccess', () => {
  it('stays out of the header until a view has been saved', () => {
    render(<SidebarSavedViewsQuickAccess />)

    expect(screen.queryByRole('button', { name: 'Saved views' })).toBeNull()
  })

  it('opens on hover and applies a saved view with one click', async () => {
    setSidebarGrouping('none')
    saveCurrentSidebarView('Overview', { id: 'overview', now: 100 })
    setSidebarGrouping('date')
    render(<SidebarSavedViewsQuickAccess />)

    fireEvent.pointerEnter(screen.getByRole('button', { name: 'Saved views' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Overview' }))

    expect($sidebarGrouping.get()).toBe('none')
  })
})
