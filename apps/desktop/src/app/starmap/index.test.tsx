// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import type { StarmapGraph } from '../../types/hermes'

import { LearningCandidates } from './learning-candidates'

const ledgerOnlyGraph: StarmapGraph = {
  nodes: [],
  edges: [],
  clusters: [],
  memory: [],
  stats: {},
  candidates: [
    {
      id: 'candidate-1',
      subsystem: 'skills',
      action: 'patch',
      status: 'active',
      summary: 'Verified retry procedure',
      risk: 'low',
      hypothesis: 'Fewer repeat failures',
      source: {},
      outcomes: []
    }
  ]
}

describe('LearningCandidates', () => {
  afterEach(() => {
    cleanup()
  })

  it('renders a ledger-only candidate instead of a blank journey', () => {
    render(<LearningCandidates graph={ledgerOnlyGraph} />)

    expect(screen.getByRole('region', { name: 'Learning candidates' })).toBeTruthy()
    expect(screen.getByText('Verified retry procedure')).toBeTruthy()
    expect(screen.getByText('active')).toBeTruthy()
    expect(screen.getByText('skills · patch · risk low')).toBeTruthy()
  })

  it('renders nothing when there are no candidates', () => {
    const { container } = render(
      <LearningCandidates graph={{ ...ledgerOnlyGraph, candidates: [] }} />
    )
    expect(container.firstChild).toBeNull()
  })
})
