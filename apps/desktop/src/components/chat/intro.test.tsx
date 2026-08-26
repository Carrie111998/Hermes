import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Intro } from './intro'

afterEach(cleanup)

describe('Intro', () => {
  it('shows a read-only session prompt in spectator mode', () => {
    render(<Intro spectator />)

    expect(screen.getByText('Select a session to view it. This iPad app is read-only.')).toBeTruthy()
    expect(screen.queryByText(/Type a task|Send a bug|Drop a file path/i)).toBeNull()
  })
})
