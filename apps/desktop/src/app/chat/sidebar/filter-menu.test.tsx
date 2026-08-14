import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $savedSidebarViews } from '@/store/sidebar-views'

import { SidebarFilterMenu } from './filter-menu'

beforeEach(() => {
  $savedSidebarViews.set({ version: 1, views: [] })
})

afterEach(cleanup)

describe('SidebarFilterMenu saved views', () => {
  it('saves the current sidebar configuration from the existing filter menu', async () => {
    render(<SidebarFilterMenu />)

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Filters' }), { button: 0, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Save current view…' }))

    expect(await screen.findByRole('dialog', { name: 'Save sidebar view' })).toBeTruthy()

    fireEvent.change(screen.getByRole('textbox', { name: 'Name' }), { target: { value: 'Review queue' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    expect($savedSidebarViews.get().views).toHaveLength(1)
    expect($savedSidebarViews.get().views[0]?.name).toBe('Review queue')
  })
})
