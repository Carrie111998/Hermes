import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Checkbox } from './checkbox'

afterEach(cleanup)

describe('Checkbox', () => {
  it('drives glyph visibility from the indicator state', () => {
    render(<Checkbox aria-label="checked" checked />)

    const checkbox = screen.getByRole('checkbox', { name: 'checked' })
    const indicator = checkbox.querySelector('[data-slot="checkbox-indicator"]')

    expect(checkbox.getAttribute('data-state')).toBe('checked')
    expect(indicator?.getAttribute('data-state')).toBe('checked')
    expect(indicator?.className).toContain('[&>.codicon]:hidden!')
    expect(indicator?.className).toContain('data-[state=checked]:[&>.codicon-check]:block!')
    expect(indicator?.className).toContain('data-[state=indeterminate]:[&>.codicon-dash]:block!')
    expect(checkbox.className.split(' ')).not.toContain('group')
  })

  it('exposes the indeterminate state to the mixed-state glyph selector', () => {
    render(<Checkbox aria-label="indeterminate" checked="indeterminate" />)

    const checkbox = screen.getByRole('checkbox', { name: 'indeterminate' })
    const indicator = checkbox.querySelector('[data-slot="checkbox-indicator"]')

    expect(checkbox.getAttribute('aria-checked')).toBe('mixed')
    expect(checkbox.getAttribute('data-state')).toBe('indeterminate')
    expect(indicator?.getAttribute('data-state')).toBe('indeterminate')
  })
})
