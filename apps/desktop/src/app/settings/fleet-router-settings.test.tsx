import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { FleetLaneEvaluation, FleetStatusResponse } from '@/types/hermes'

import { FleetRouterSettings } from './fleet-router-settings'

const { getFleetStatus } = vi.hoisted(() => ({ getFleetStatus: vi.fn() }))

vi.mock('@/hermes', () => ({
  getFleetStatus: (profile?: string) => getFleetStatus(profile)
}))

vi.mock('@/store/profile', async () => {
  const { atom } = await import('nanostores')

  return {
    $activeGatewayProfile: atom('default'),
    normalizeProfileKey: (name: null | string | undefined) => name?.trim() || 'default'
  }
})

function lane(laneId: string, patch: Partial<FleetLaneEvaluation> = {}): FleetLaneEvaluation {
  return {
    adapter_kind: 'external_cli',
    capacity: null,
    effort: 'high',
    eligible: false,
    enabled: true,
    fallback_eligible: false,
    lane_id: laneId,
    model_id: `${laneId}-model`,
    provider_id: `${laneId}-provider`,
    qualification_detail: `Qualified ${laneId}`,
    qualification_evidence_id: `evidence:${laneId}`,
    reasons: ['QUALIFICATION_FAILED'],
    selectable: false,
    ...patch
  }
}

function fleetPayload(patch: Partial<FleetStatusResponse> = {}): FleetStatusResponse {
  return {
    command: 'doctor',
    enabled: true,
    evaluations: [
      lane('chatgpt_codex', {
        adapter_kind: 'native_provider',
        capacity: {
          captured_at: '2026-07-24T05:00:00+00:00',
          confidence: 'high',
          effective_remaining_pct: '75.000',
          expires_at: '2026-07-24T05:05:00+00:00',
          freshness: 'fresh',
          lane_id: 'chatgpt_codex',
          overage_disabled: true,
          read_at: '2026-07-24T05:00:00+00:00',
          remaining_pct: '80.000',
          reserved_pct: '5.000',
          schema_version: '1',
          source_hash: 'abc123',
          source_id: 'bridge#abc123',
          source_kind: 'bridge_file',
          used_pct: '20.000'
        },
        eligible: true,
        model_id: 'gpt-5.6-sol',
        provider_id: 'openai-codex',
        qualification_detail: 'Codex subscription is qualified.',
        reasons: ['MET'],
        selectable: true
      }),
      lane('claude_code', {
        enabled: false,
        reasons: ['LANE_DISABLED'],
        qualification_detail: 'Claude CLI subscription detected.'
      }),
      lane('grok', {
        adapter_kind: 'native_provider',
        reasons: ['AUTH_MISSING']
      }),
      lane('antigravity', {
        capacity: {
          captured_at: '2026-07-23T05:00:00+00:00',
          confidence: 'high',
          effective_remaining_pct: '64.000',
          expires_at: '2026-07-23T05:05:00+00:00',
          freshness: 'stale',
          lane_id: 'antigravity',
          overage_disabled: true,
          read_at: '2026-07-24T05:00:00+00:00',
          remaining_pct: '64.000',
          reserved_pct: '0.000',
          schema_version: '1',
          source_hash: 'stale123',
          source_id: 'bridge#stale123',
          source_kind: 'bridge_file',
          used_pct: '36.000'
        },
        fallback_eligible: true,
        reasons: ['ROTATION_WITHOUT_FRESH_CAPACITY', 'CAPACITY_STALE'],
        selectable: true
      }),
      lane('kimi', {
        effort: null,
        enabled: false,
        model_id: null,
        reasons: ['LANE_DISABLED', 'ADAPTER_UNIMPLEMENTED']
      })
    ],
    ok: true,
    reason: 'MET',
    schema_version: 1,
    ...patch
  }
}

function renderFleet() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } }
  })

  return render(
    <QueryClientProvider client={client}>
      <FleetRouterSettings />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  getFleetStatus.mockResolvedValue(fleetPayload())
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('FleetRouterSettings', () => {
  it('renders global health and all five authoritative lane evaluations', async () => {
    renderFleet()

    expect(await screen.findByText('Fleet enabled')).toBeTruthy()
    expect(screen.getByText('Healthy')).toBeTruthy()

    for (const name of ['ChatGPT / Codex', 'Claude Code', 'Grok', 'Antigravity', 'Kimi']) {
      expect(screen.getByRole('article', { name })).toBeTruthy()
    }

    const codex = within(screen.getByRole('article', { name: 'ChatGPT / Codex' }))
    expect(codex.getByText('75.000%')).toBeTruthy()
    expect(codex.getByText(/native_provider/)).toBeTruthy()
    expect(codex.getByText('Codex subscription is qualified.')).toBeTruthy()
    expect(codex.getByText('MET')).toBeTruthy()

    const antigravity = within(screen.getByRole('article', { name: 'Antigravity' }))
    expect(antigravity.getByText('Enabled')).toBeTruthy()
    expect(antigravity.getByText('In rotation')).toBeTruthy()
    expect(antigravity.queryByText('Disabled')).toBeNull()
    expect(antigravity.getByText(/normal rotation, not provider-failure fallback/)).toBeTruthy()
    expect(antigravity.getByText('stale')).toBeTruthy()
  })

  it('shows an honest loading state while server truth is pending', async () => {
    let resolve!: (payload: FleetStatusResponse) => void
    getFleetStatus.mockReturnValue(
      new Promise<FleetStatusResponse>(done => {
        resolve = done
      })
    )

    renderFleet()

    expect(await screen.findByRole('status', { name: 'Checking fleet routes…' })).toBeTruthy()

    await act(async () => resolve(fleetPayload()))
  })

  it('distinguishes an older backend without the endpoint from a transient error', async () => {
    getFleetStatus.mockRejectedValue(new Error('404: {"detail":"Not Found"}'))

    renderFleet()

    expect(await screen.findByText('Fleet status is unavailable')).toBeTruthy()
    expect(screen.getByText(/backend predates the Fleet Router status endpoint/)).toBeTruthy()
  })

  it('shows a retryable error with the backend failure detail', async () => {
    getFleetStatus.mockRejectedValue(new Error('503: capacity service offline'))

    renderFleet()

    expect(await screen.findByText('Fleet status failed to load')).toBeTruthy()
    expect(screen.getByText(/503: capacity service offline/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })

  it('refreshes from the active profile and replaces cached presentation with server truth', async () => {
    getFleetStatus.mockResolvedValueOnce(fleetPayload()).mockResolvedValueOnce(
      fleetPayload({
        evaluations: fleetPayload().evaluations.map(item =>
          item.lane_id === 'chatgpt_codex' && item.capacity
            ? { ...item, capacity: { ...item.capacity, effective_remaining_pct: '62.000' } }
            : item
        )
      })
    )

    renderFleet()

    expect(await screen.findByText('75.000%')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(getFleetStatus).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('62.000%')).toBeTruthy()
    expect(getFleetStatus).toHaveBeenLastCalledWith('default')
  })
})
