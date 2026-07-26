import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { GovernanceSettings } from './governance-settings'

const decide = vi.fn()
const revoke = vi.fn()

vi.mock('@/hermes', () => ({
  decideGovernanceApproval: (...args: unknown[]) => decide(...args),
  getGovernanceApprovals: vi.fn(async () => ({
    count: 1,
    approvals: [{
      id: 'a1', session_key: 'cron:test', tool_name: 'send_message', risk_class: 'external',
      target: 'discord:#ops', args_digest: 'abc', args_preview: '{"text":"daily"}',
      reason: 'daily report', pattern_key: 'daily', status: 'pending', source: 'plugin',
      created_at: 1, expires_at: 9999999999, integrity_ok: true
    }]
  })),
  getGovernanceConnectors: vi.fn(async () => ({
    count: 1,
    connectors: [{ id: 'mcp-github', enabled: true, health: 'healthy', tool_count: 2,
      available_tool_count: 2, tools: ['issues_get', 'issues_create'],
      risk_classes: ['read', 'external'], highest_risk: 'external' }]
  })),
  getGovernanceRules: vi.fn(async () => ({
    count: 1,
    rules: [{ id: 'r1', tool_name: 'send_message', operation: '', target_pattern: 'discord:#ops',
      risk_ceiling: 'external', profile: '', workspace: '', job_id: '', use_count: 1,
      enabled: true, note: '', created_at: 1 }]
  })),
  revokeGovernanceRule: (...args: unknown[]) => revoke(...args)
}))

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><GovernanceSettings /></QueryClientProvider>)
}

describe('GovernanceSettings', () => {
  beforeEach(() => { decide.mockReset(); revoke.mockReset(); decide.mockResolvedValue({ resolved: true }); revoke.mockResolvedValue({ revoked: true }) })

  it('shows pending approvals, bounded rules, and connector health', async () => {
    renderPage()
    expect(await screen.findByText('daily report')).toBeTruthy()
    expect(document.body.textContent).toContain('discord:#ops')
    expect(screen.getByText('mcp-github')).toBeTruthy()
    expect(screen.getByText('healthy')).toBeTruthy()
  })

  it('submits an exact one-time approval decision', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Allow once' }))
    await waitFor(() => expect(decide).toHaveBeenCalledWith('a1', 'allow-once'))
  })

  it('revokes a standing rule', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(revoke.mock.calls[0]?.[0]).toBe('r1'))
  })
})
