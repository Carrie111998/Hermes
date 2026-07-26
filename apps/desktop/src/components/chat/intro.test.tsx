// @vitest-environment jsdom
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Intro } from './intro'

describe('Intro', () => {
  it('renders the approved Atlas subtitle and readiness pill', () => {
    render(<Intro />)

    expect(screen.getByLabelText('HERMES AGENT')).toBeTruthy()
    expect(
      screen.getByText(
        "Drop a file path, a traceback, or a rough idea. I'll investigate, suggest next steps, and keep things reversible."
      )
    ).toBeTruthy()
    expect(screen.getByText('Ready when you are')).toBeTruthy()
  })
})
