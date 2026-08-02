// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { StarmapGraph } from '../../types/hermes'

import { LearningCandidates } from './learning-candidates'
import { StarmapView } from './index'

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

// Mock the store module so StarmapView composition tests don't pull in the
// @/-aliased dependency chain (only resolvable under the desktop vite config).
// The factory is hoisted, so atoms are created inside and re-imported below.
vi.mock('../../store/starmap', async () => {
  const { atom } = await import('nanostores')
  return {
    $starmapGraph: atom(null),
    $starmapLoading: atom(false),
    $starmapError: atom(null),
    loadStarmapGraph: vi.fn(),
  }
})

// Import the mocked module to control atom values per test.
import { $starmapGraph as $mockGraph, $starmapLoading as $mockLoading, $starmapError as $mockError } from '../../store/starmap'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      starmap: {
        close: 'Close',
        loadFailed: 'Load failed',
        loading: 'Loading',
        emptyDesc: 'Nothing learned yet',
        emptyTitle: 'Empty journey',
      }
    }
  })
}))

vi.mock('@/components/page-loader', () => ({
  PageLoader: () => <div data-testid="page-loader">Loading...</div>,
}))

vi.mock('../overlays/panel', () => ({
  Panel: ({ children }: { children: React.ReactNode }) => <div data-testid="panel">{children}</div>,
  PanelEmpty: ({ title }: { title: string }) => <div data-testid="panel-empty">{title}</div>,
}))

vi.mock('./star-map', () => ({
  StarMap: () => <div data-testid="star-map" />,
}))

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

describe('StarmapView composition', () => {
  afterEach(() => {
    cleanup()
    $mockGraph.set(null)
    $mockLoading.set(false)
    $mockError.set(null)
  })

  it('mounts LearningCandidates and does not show the empty state for a ledger-only graph', () => {
    $mockGraph.set(ledgerOnlyGraph)

    render(<StarmapView onClose={() => undefined} />)

    // The candidate section is actually mounted (not dropped from composition).
    expect(screen.getByRole('region', { name: 'Learning candidates' })).toBeTruthy()
    expect(screen.getByText('Verified retry procedure')).toBeTruthy()
    // The empty-state gate treats ledger-only content as real content.
    expect(screen.queryByTestId('panel-empty')).toBeNull()
  })
})
