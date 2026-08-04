import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Intro } from './intro'
import introCopyJsonl from './intro-copy.jsonl?raw'

describe('managed evaOS Agent intro branding', () => {
  it('renders the evaOS wordmark without upstream product branding', () => {
    render(<Intro personality="none" seed={0} />)

    expect(screen.getByLabelText('evaOS AGENT')).not.toBeNull()
    expect(screen.queryByText(/hermes agent/i)).toBeNull()
  })

  it('keeps the seeded default headline on the managed product name', () => {
    const defaultRecord = introCopyJsonl
      .split(/\r?\n/)
      .map(line => line.trim())
      .filter(Boolean)
      .map(line => JSON.parse(line) as { headline: string; personality: string })
      .find(record => record.personality === 'none')

    expect(defaultRecord?.headline).toBe('evaOS Agent is ready.')
  })
})
