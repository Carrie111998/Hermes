// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'
import { HUB_SOURCES_KEY } from '@/store/hub-actions'

import { SkillsHub } from './hub'
import { getSkillHubQueryState } from './hub-query-state'

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  getSkillHubSources: vi.fn(),
  searchSkillsHub: vi.fn()
}))

vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('getSkillHubQueryState', () => {
  it('shows featured skills for an empty input', () => {
    expect(getSkillHubQueryState('  ', 'previous')).toEqual({
      pending: false,
      showLanding: true,
      showResults: false
    })
  })

  it('shows results only after the debounced term matches the input', () => {
    expect(getSkillHubQueryState('matrix', 'matrix')).toEqual({
      pending: false,
      showLanding: false,
      showResults: true
    })
  })

  it('hides stale rows and actions until the changed query settles', async () => {
    vi.useFakeTimers()

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const result = (name: string, identifier: string) => ({
      name,
      identifier,
      description: `${name} description`,
      installed: {},
      repo: null,
      results: [],
      source: 'official',
      source_counts: {},
      tags: [],
      timed_out: [],
      trust_level: 'trusted'
    })

    const matrix = result('Matrix Skill', 'matrix-skill')
    const telegram = result('Telegram Skill', 'telegram-skill')

    client.setQueryData(HUB_SOURCES_KEY, {
      featured: [],
      index_available: true,
      installed: {},
      sources: [{ id: 'official', label: 'Official', searchable: true }]
    })
    client.setQueryData(['skill-hub-search', 'matrix', 'official'], { ...matrix, results: [matrix] })
    client.setQueryData(['skill-hub-search', 'telegram', 'official'], { ...telegram, results: [telegram] })

    const view = (query: string) => createElement(QueryClientProvider, { client }, createElement(SkillsHub, { query }))

    const rendered = render(view('matrix'))

    await act(async () => {})
    expect(screen.getByText('Matrix Skill')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Preview' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Install' })).toBeTruthy()

    rendered.rerender(view('telegram'))

    expect(screen.queryByText('Matrix Skill')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Preview' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Install' })).toBeNull()

    await act(() => vi.advanceTimersByTimeAsync(350))
    expect(screen.getByText('Telegram Skill')).toBeTruthy()
  })
})
