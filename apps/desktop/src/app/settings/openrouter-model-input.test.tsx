import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { OpenRouterModelInput } from './openrouter-model-input'

afterEach(cleanup)

const OPTIONS = ['deepseek/deepseek-v4-flash', 'openai/gpt-5.4', 'anthropic/claude-sonnet-4-6'] as const

function ControlledInput({ initialValue }: { initialValue: string }) {
  const [value, setValue] = useState(initialValue)

  return <OpenRouterModelInput hint="author/slug" label="OpenRouter model" onChange={setValue} options={OPTIONS} value={value} />
}

describe('OpenRouterModelInput', () => {
  it('shows a visible searchable affordance in the closed state', async () => {
    render(
      <OpenRouterModelInput
        hint="author/slug"
        label="OpenRouter model"
        onChange={vi.fn()}
        options={OPTIONS}
        value=""
      />
    )

    const input = screen.getByRole('combobox', { name: 'OpenRouter model' })
    // Accessible contract: role=combobox with the standard listbox wiring —
    // asserted regardless of any visual affordance below.
    expect(input.getAttribute('role')).toBe('combobox')
    expect(input.hasAttribute('aria-controls')).toBe(true)
    expect(input.hasAttribute('aria-expanded')).toBe(true)
    // Visible cue that this field opens a searchable list even when closed
    // and pre-filled with a complete value (issue C: no chevron/icon meant
    // a complete model ID looked like a plain, non-interactive text field).
    expect(document.querySelector('[data-search-affordance]')).toBeTruthy()
  })

  it('shows the full option list on focus when pre-filled with a complete model id, not just a self-match', async () => {
    render(
      <OpenRouterModelInput
        hint="author/slug"
        label="OpenRouter model"
        onChange={vi.fn()}
        options={OPTIONS}
        value="deepseek/deepseek-v4-flash"
      />
    )

    const input = screen.getByRole('combobox', { name: 'OpenRouter model' })
    fireEvent.focus(input)

    // All three options must be visible — a substring filter against the
    // complete pre-filled value would show only the one row matching itself.
    expect(await screen.findByRole('option', { name: 'deepseek/deepseek-v4-flash' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'openai/gpt-5.4' })).toBeTruthy()
    expect(screen.getByRole('option', { name: 'anthropic/claude-sonnet-4-6' })).toBeTruthy()
  })

  it('filters to matches once the user edits the pre-filled value', async () => {
    render(<ControlledInput initialValue="deepseek/deepseek-v4-flash" />)

    const input = screen.getByRole('combobox', { name: 'OpenRouter model' })
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'gpt-5' } })

    expect(await screen.findByRole('option', { name: 'openai/gpt-5.4' })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'deepseek/deepseek-v4-flash' })).toBeNull()
  })
})
