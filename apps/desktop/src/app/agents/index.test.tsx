import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type * as Nanostores from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { getCronJobs, getProfiles } from '@/hermes'
import type { CronJob, ProfileInfo } from '@/types/hermes'

import { AgentsView } from './index'

afterEach(cleanup)

vi.mock('@/hermes', () => ({
  getCronJobs: vi.fn(async () => []),
  getProfiles: vi.fn(async () => ({ profiles: [] }))
}))

const { $subagentsBySession } = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof Nanostores

  return {
    $subagentsBySession: atom({})
  }
})

vi.mock('@/store/subagents', () => ({
  $subagentsBySession,
  allSubagents: vi.fn(() => []),
  buildSubagentTree: vi.fn(() => [])
}))

function profile(name: string, isDefault = false): ProfileInfo {
  return {
    has_env: false,
    is_default: isDefault,
    model: isDefault ? 'default-model' : 'worker-model',
    name,
    path: `/home/user/.hermes/${isDefault ? '' : `profiles/${name}/`}`,
    provider: 'openrouter',
    skill_count: isDefault ? 12 : 4
  }
}

function cronJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    deliver: 'local',
    enabled: true,
    id: 'job-1',
    name: 'Morning digest',
    next_run_at: '2026-08-01T09:00:00Z',
    prompt: 'Summarize overnight work and report only material changes.',
    schedule: { display: 'Daily at 9 AM', expr: '0 9 * * *', kind: 'cron' },
    ...overrides
  }
}

async function renderAgentsView() {
  await act(async () => {
    render(<AgentsView onClose={vi.fn()} />)
  })
}

describe('AgentsView local agents board', () => {
  it('auto-discovers local profiles and opens the default profile detail', async () => {
    vi.mocked(getProfiles).mockResolvedValue({ profiles: [profile('worker'), profile('default', true)] })
    vi.mocked(getCronJobs).mockResolvedValue([cronJob()])

    await renderAgentsView()

    expect(await screen.findByRole('button', { name: /default/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /worker/i })).toBeTruthy()

    await waitFor(() => expect(getCronJobs).toHaveBeenCalledWith('default'))
    expect(await screen.findByText('Morning digest')).toBeTruthy()
    expect(screen.getByText('Daily at 9 AM')).toBeTruthy()
    expect(screen.getByText('Summarize overnight work and report only material changes.')).toBeTruthy()
  })

  it('loads the selected agent cron jobs and renders a default metadata view', async () => {
    vi.mocked(getProfiles).mockResolvedValue({ profiles: [profile('default', true), profile('worker')] })
    vi.mocked(getCronJobs).mockImplementation(async requestedProfile =>
      requestedProfile === 'worker'
        ? [cronJob({ deliver: 'discord', id: 'worker-job', model: 'gpt-5.5', provider: 'openai', script: 'scripts/check.py' })]
        : []
    )

    await renderAgentsView()

    fireEvent.click(await screen.findByRole('button', { name: /worker/i }))

    await waitFor(() => expect(getCronJobs).toHaveBeenCalledWith('worker'))
    const article = (await screen.findByText('worker-job')).closest('article')
    expect(article).not.toBeNull()
    expect(within(article!).getByText('discord')).toBeTruthy()
    expect(within(article!).getByText('openai/gpt-5.5')).toBeTruthy()
    expect(within(article!).getByText('script')).toBeTruthy()
  })
})
