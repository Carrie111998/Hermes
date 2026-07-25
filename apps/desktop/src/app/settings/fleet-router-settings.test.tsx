import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { FleetLaneEvaluation, FleetStatusResponse } from '@/types/hermes'

import { FleetRouterSettings } from './fleet-router-settings'

const { getFleetStatus, getHermesConfigRecordForProfile, saveHermesConfigForProfile } = vi.hoisted(() => ({
  getFleetStatus: vi.fn(),
  getHermesConfigRecordForProfile: vi.fn(),
  saveHermesConfigForProfile: vi.fn()
}))

vi.mock('@/hermes', () => ({
  getFleetStatus: (profile?: string) => getFleetStatus(profile),
  getHermesConfigRecordForProfile: (profile: string) => getHermesConfigRecordForProfile(profile),
  saveHermesConfigForProfile: (config: unknown, profile: string) => saveHermesConfigForProfile(config, profile)
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
  const workers = [
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
      route_purpose: 'task_worker',
      selectable: true,
      supports_parent_session: true,
      supports_task_worker: true
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
      model_id: 'gemini-3.1-pro-high',
      model_label: 'Gemini 3.1 Pro (High)',
      reasons: ['ROTATION_WITHOUT_FRESH_CAPACITY', 'CAPACITY_STALE'],
      route_purpose: 'task_worker',
      selectable: true,
      supports_parent_session: true,
      supports_task_worker: true
    }),
    lane('kimi', {
      effort: null,
      enabled: false,
      model_id: null,
      reasons: ['LANE_DISABLED', 'ADAPTER_UNIMPLEMENTED']
    })
  ]

  const parents = workers.map(item =>
    item.lane_id === 'antigravity'
      ? {
          ...item,
          eligible: true,
          fallback_eligible: false,
          reasons: ['ROTATION_WITHOUT_FRESH_CAPACITY', 'CAPACITY_STALE'],
          route_purpose: 'desktop_parent' as const,
          selectable: true
        }
      : { ...item, route_purpose: 'desktop_parent' as const }
  )

  return {
    command: 'doctor',
    enabled: true,
    evaluations: workers,
    ok: true,
    purposes: {
      desktop_parent: {
        eligible: true,
        enabled: true,
        evaluations: parents,
        route_purpose: 'desktop_parent'
      },
      task_worker: {
        eligible: true,
        enabled: true,
        evaluations: workers,
        route_purpose: 'task_worker'
      }
    },
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
  getHermesConfigRecordForProfile.mockResolvedValue({ fleet: { enabled: true, parent_desktop_enabled: true } })
  saveHermesConfigForProfile.mockResolvedValue({ ok: true })
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
      expect(screen.getByRole('article', { name: `${name} · Worker` })).toBeTruthy()
      expect(screen.getByRole('article', { name: `${name} · Parent` })).toBeTruthy()
    }

    const codex = within(screen.getByRole('article', { name: 'ChatGPT / Codex · Worker' }))
    expect(codex.getByText('75.000%')).toBeTruthy()
    expect(codex.getByText(/native_provider/)).toBeTruthy()
    expect(codex.getByText('Codex subscription is qualified.')).toBeTruthy()
    expect(codex.getByText('MET')).toBeTruthy()

    const antigravity = within(screen.getByRole('article', { name: 'Antigravity · Worker' }))
    expect(antigravity.getByText('Enabled')).toBeTruthy()
    expect(antigravity.getByText('In rotation')).toBeTruthy()
    expect(antigravity.queryByText('Disabled')).toBeNull()
    expect(antigravity.getByText(/normal rotation, not provider-failure fallback/)).toBeTruthy()
    expect(antigravity.getByText('stale')).toBeTruthy()
    expect(antigravity.getByText('Gemini 3.1 Pro (High)')).toBeTruthy()

    const antigravityParent = within(screen.getByRole('article', { name: 'Antigravity · Parent' }))
    expect(antigravityParent.getByText('Selectable')).toBeTruthy()
    expect(antigravityParent.getByText('Gemini 3.1 Pro (High)')).toBeTruthy()
    expect(antigravityParent.getByText(/External CLI/)).toBeTruthy()
  })

  it('updates Fleet Auto for the active profile without replacing unrelated config', async () => {
    getFleetStatus.mockResolvedValue(
      fleetPayload({
        purposes: {
          ...fleetPayload().purposes,
          desktop_parent: {
            ...fleetPayload().purposes!.desktop_parent!,
            route_purpose: 'desktop_parent',
            enabled: false
          }
        }
      })
    )
    renderFleet()

    fireEvent.click(await screen.findByRole('switch', { name: 'Fleet Auto for new Desktop sessions' }))

    await waitFor(() =>
      expect(saveHermesConfigForProfile).toHaveBeenCalledWith(
        {
          fleet: { enabled: true, parent_desktop_enabled: true }
        },
        'default'
      )
    )
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
    const refreshed = fleetPayload()

    const refreshedWorkers = refreshed.purposes!.task_worker!.evaluations.map(item =>
      item.lane_id === 'chatgpt_codex' && item.capacity
        ? { ...item, capacity: { ...item.capacity, effective_remaining_pct: '62.000' } }
        : item
    )

    getFleetStatus.mockResolvedValueOnce(fleetPayload()).mockResolvedValueOnce(
      fleetPayload({
        evaluations: refreshedWorkers,
        purposes: {
          ...refreshed.purposes,
          task_worker: {
            ...refreshed.purposes!.task_worker!,
            evaluations: refreshedWorkers
          }
        }
      })
    )

    renderFleet()

    const codexWorker = within(await screen.findByRole('article', { name: 'ChatGPT / Codex · Worker' }))
    expect(codexWorker.getByText('75.000%')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() => expect(getFleetStatus).toHaveBeenCalledTimes(2))
    expect(
      await within(screen.getByRole('article', { name: 'ChatGPT / Codex · Worker' })).findByText('62.000%')
    ).toBeTruthy()
    expect(getFleetStatus).toHaveBeenLastCalledWith('default')
  })
})
