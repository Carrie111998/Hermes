import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { NewSessionHeaderButton } from './new-session-header-button'

afterEach(cleanup)

// #83479: the Home section header rendered only its "back to projects"
// button, with no affordance to start a new session — this asserts the
// button is actually there and wired up, not just that some handler exists.
describe('NewSessionHeaderButton', () => {
  it('renders a button with the given label and fires onClick', () => {
    const onClick = vi.fn()
    render(<NewSessionHeaderButton label="New session" onClick={onClick} />)

    const button = screen.getByRole('button', { name: 'New session' })
    button.click()

    expect(onClick).toHaveBeenCalledOnce()
  })

  it('wraps the button in a Tip with the label as the trigger', () => {
    render(<NewSessionHeaderButton label="New session" onClick={vi.fn()} />)

    const button = screen.getByRole('button', { name: 'New session' })
    expect(button.closest('[data-slot="tooltip-trigger"]')).toBeTruthy()
  })
})
