import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SidebarRowLabel, SidebarRowLink } from '@/app/chat/sidebar/chrome'

import { SearchField } from './search-field'

vi.mock('@/i18n', () => ({
  useI18n: () => ({ t: { ui: { search: { clear: 'Clear search' } } } })
}))

afterEach(cleanup)

describe('content-aware direction', () => {
  it('lets shared search fields resolve Hebrew and mixed queries from their content', () => {
    render(<SearchField onChange={vi.fn()} placeholder="Search" value="שלום Hermes" />)

    const input = screen.getByRole('textbox', { name: 'Search' })
    expect(input.getAttribute('dir')).toBe('auto')
    expect(input.classList.contains('text-start')).toBe(true)
  })

  it('keeps search direction automatic as the query changes', () => {
    const onChange = vi.fn()
    render(<SearchField onChange={onChange} placeholder="Search" value="" />)

    fireEvent.change(screen.getByRole('textbox', { name: 'Search' }), { target: { value: 'Hermes בעברית' } })
    expect(onChange).toHaveBeenCalledWith('Hermes בעברית')
  })

  it('lets shared sidebar labels resolve their own direction without mirroring row chrome', () => {
    render(
      <>
        <SidebarRowLabel>ניהול גרסאות Hermes</SidebarRowLabel>
        <SidebarRowLink>פרויקט Daber Elai</SidebarRowLink>
      </>
    )

    const label = screen.getByText('ניהול גרסאות Hermes')
    const link = screen.getByText('פרויקט Daber Elai')

    expect(label.getAttribute('dir')).toBe('auto')
    expect(label.classList.contains('text-start')).toBe(true)
    expect(link.getAttribute('dir')).toBe('auto')
    expect(link.classList.contains('text-start')).toBe(true)
  })
})
