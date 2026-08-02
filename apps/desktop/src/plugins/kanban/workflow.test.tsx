import { host } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { bindApi, fetchWorkflow, pauseWorkflow } from './api'
import type { WorkflowProjection } from './types'
import { WorkflowPage } from './workflow'

const projection: WorkflowProjection = {
  projection: 'workflow-runtime-v1',
  canonical_source: 'github',
  board: 'veltro-roadmap',
  server_time: 1_700_000_010,
  controller: {
    version: 7,
    dispatch_enabled: true,
    broker_ready: false,
    status: 'healthy',
    controller_epoch: 'epoch-remote',
    heartbeat_at: 1_700_000_000,
    last_reconciled_at: 1_700_000_000,
    updated_at: 1_700_000_000
  },
  leaves: [
    {
      id: 't_remote',
      title: 'Remote leaf',
      status: 'running',
      leaf_key: 'github:veltrosecurity/veltro:issue-257:leaf-remote:v1',
      leaf_family_key: 'github:veltrosecurity/veltro:issue-257:leaf-remote',
      specification_version: 'v1',
      spec_hash: 'a'.repeat(64),
      pin_sha: 'b'.repeat(40),
      capsule_hash: 'c'.repeat(64),
      canonical: { source: 'github', repository: 'veltrosecurity/veltro', campaign_issue: 257 },
      dependencies: []
    }
  ]
}

function wrapper(client: QueryClient) {
  return function QueryWrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

describe('WorkflowPage remote reconnect controls', () => {
  let dispose: () => void
  let rest: ReturnType<typeof vi.fn>

  beforeEach(() => {
    rest = vi.fn(async (path: string, options?: { method?: string }) => {
      if (path === '/workflow/projection?board=veltro-roadmap' && !options?.method) {
        return projection
      }

      if (path === '/workflow/controller/pause?board=veltro-roadmap') {
        throw new Error('remote unavailable')
      }

      throw new Error(`unexpected request: ${options?.method ?? 'GET'} ${path}`)
    })

    dispose = bindApi(
      rest as never,
      {
        get: (key: string, fallback: unknown) => (key === 'boardSlug' ? 'veltro-roadmap' : fallback),
        set: vi.fn()
      } as never,
      (() => () => undefined) as never
    )
    vi.spyOn(host, 'notify').mockImplementation(() => 'workflow-test-notification')
  })

  afterEach(() => {
    dispose()
    vi.restoreAllMocks()
  })

  it('fetches fresh remote state on remount and never replays a failed control', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const first = render(<WorkflowPage />, { wrapper: wrapper(client) })

    expect(await screen.findByText('Remote leaf')).toBeTruthy()
    expect(rest.mock.calls.filter(([path]) => path === '/workflow/projection?board=veltro-roadmap')).toHaveLength(1)

    const reason = screen.getByLabelText('Workflow control reason')
    fireEvent.change(reason, { target: { value: 'operator safety pause' } })
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))

    await waitFor(() => {
      expect(rest.mock.calls.filter(([path]) => path === '/workflow/controller/pause?board=veltro-roadmap')).toHaveLength(1)
    })
    expect(rest).toHaveBeenCalledWith('/workflow/controller/pause?board=veltro-roadmap', {
      method: 'POST',
      body: { expected_version: 7, reason: 'operator safety pause' }
    })
    expect((screen.getByRole('button', { name: 'Resume' }) as HTMLButtonElement).disabled).toBe(true)

    first.unmount()
    render(<WorkflowPage />, { wrapper: wrapper(client) })

    await waitFor(() => {
      expect(rest.mock.calls.filter(([path]) => path === '/workflow/projection?board=veltro-roadmap')).toHaveLength(2)
    })
    expect(rest.mock.calls.filter(([path]) => path === '/workflow/controller/pause?board=veltro-roadmap')).toHaveLength(1)
  })

  it('rejects a malformed remote projection at the runtime boundary', async () => {
    rest.mockResolvedValueOnce({ schema: 'hermes.workflow-runtime-projection.v1', leaves: [] })

    await expect(fetchWorkflow()).rejects.toThrow('invalid Workflow projection')
  })

  it('rejects a malformed nested current run at the runtime boundary', async () => {
    rest.mockResolvedValueOnce({
      ...projection,
      leaves: [
        {
          ...projection.leaves[0],
          current_run: { id: 9, status: 'running', fence_digest: 42 }
        }
      ]
    })

    await expect(fetchWorkflow()).rejects.toThrow('invalid Workflow projection')
  })

  it('rejects a malformed controller operation response', async () => {
    rest.mockResolvedValueOnce({
      controller: { ...projection.controller, version: 'stale' }
    })

    await expect(pauseWorkflow(7, 'bounded pause')).rejects.toThrow('invalid Workflow controller response')
  })
})
