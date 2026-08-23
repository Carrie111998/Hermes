import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n/context'

const api = vi.hoisted(() => ({
  getActionStatus: vi.fn(),
  getLogs: vi.fn(),
  getStatus: vi.fn(),
  getUsageAnalytics: vi.fn(),
  restartGateway: vi.fn(),
  updateHermes: vi.fn()
}))

const activity = vi.hoisted(() => ({ upsertDesktopActionTask: vi.fn() }))

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal()),
  ...api
}))
vi.mock('@/store/activity', () => activity)

import { CommandCenterView } from './index'

async function renderSystemView() {
  api.getStatus.mockResolvedValue({
    active_sessions: 0,
    gateway_running: true,
    version: '1.0.0'
  })
  api.getLogs.mockResolvedValue({ lines: [] })

  await act(async () => {
    render(
      <I18nProvider configClient={{ getConfig: async () => ({}), saveConfig: async () => ({ ok: true }) }}>
        <MemoryRouter>
          <CommandCenterView
            initialSection="system"
            onClose={() => undefined}
            onDeleteSession={async () => undefined}
            onOpenSession={() => undefined}
          />
        </MemoryRouter>
      </I18nProvider>
    )
  })

  return screen.findByRole('button', { name: 'Update Hermes' })
}

describe('CommandCenter update action identity', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('renders typed guidance immediately and never polls a null-PID action', async () => {
    const message = 'This Hermes deployment is image-managed.'
    const command = 'docker compose pull && docker compose up -d --force-recreate'
    api.updateHermes.mockResolvedValue({
      error: 'image_managed_update_refused',
      message,
      name: 'hermes-update',
      ok: false,
      pid: null,
      update_command: command
    })

    fireEvent.click(await renderSystemView())

    await waitFor(() => {
      expect(screen.getByText(new RegExp(message))).toBeTruthy()
      expect(screen.getByText(new RegExp(command.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))).toBeTruthy()
    })
    expect(api.getActionStatus).not.toHaveBeenCalled()
    expect(api.getStatus).toHaveBeenCalledTimes(1)
  })

  it('publishes and settles a status carrying the matching POST action id', async () => {
    const actionId = 'a'.repeat(32)

    const status = {
      action_id: actionId,
      exit_code: 0,
      lines: ['matching generation complete'],
      name: 'hermes-update',
      pid: null,
      running: false
    }

    api.updateHermes.mockResolvedValue({ action_id: actionId, name: 'hermes-update', ok: true, pid: 1 })
    api.getActionStatus.mockResolvedValue(status)

    const button = await renderSystemView()
    vi.useFakeTimers()
    fireEvent.click(button)
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200)
    })

    expect(activity.upsertDesktopActionTask).toHaveBeenCalledTimes(1)
    expect(activity.upsertDesktopActionTask).toHaveBeenCalledWith(status)
  })

  it.each([
    [
      'different action id',
      {
        action_id: 'b'.repeat(32),
        exit_code: 0,
        lines: ['STALE-DIFFERENT-ID'],
        name: 'hermes-update',
        pid: null,
        running: false
      }
    ],
    [
      'missing action id without durable proof',
      {
        exit_code: 0,
        lines: ['STALE-MISSING-ID'],
        name: 'hermes-update',
        pid: null,
        running: false
      }
    ],
    [
      'known mismatch even when the shared tail contains our completion marker',
      {
        action_id: 'b'.repeat(32),
        exit_code: 0,
        lines: ['STALE-MARKER-TAIL', `=== hermes-update completed ${'a'.repeat(32)} ===`],
        name: 'hermes-update',
        pid: null,
        running: false
      }
    ]
  ])('does not publish or settle a %s', async (_label, staleStatus) => {
    const actionId = 'a'.repeat(32)

    const matchingStatus = {
      action_id: actionId,
      exit_code: 0,
      lines: ['matching generation complete'],
      name: 'hermes-update',
      pid: null,
      running: false
    }

    api.updateHermes.mockResolvedValue({ action_id: actionId, name: 'hermes-update', ok: true, pid: 1 })
    api.getActionStatus.mockResolvedValueOnce(staleStatus).mockResolvedValueOnce(matchingStatus)

    const button = await renderSystemView()
    vi.useFakeTimers()
    fireEvent.click(button)
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200)
    })

    expect(activity.upsertDesktopActionTask).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200)
    })
    expect(activity.upsertDesktopActionTask).toHaveBeenCalledTimes(1)
    expect(activity.upsertDesktopActionTask).toHaveBeenCalledWith(matchingStatus)
  })

  it.each([
    [
      'exact durable receipt',
      {
        exit_code: 2,
        lines: ['STALE-RECEIPT-TAIL'],
        name: 'hermes-update',
        pid: null,
        receipt: {
          correlation_id: 'a'.repeat(32),
          finished_at: '2026-08-23T15:00:01Z',
          outcome: 'refused',
          started_at: '2026-08-23T15:00:00Z'
        },
        running: false
      },
      2,
      'STALE-RECEIPT-TAIL'
    ],
    [
      'exact durable completion marker',
      {
        exit_code: null,
        lines: ['STALE-MARKER-TAIL', `=== hermes-update completed ${'a'.repeat(32)} ===`],
        name: 'hermes-update',
        pid: null,
        running: false
      },
      0,
      'STALE-MARKER-TAIL'
    ]
  ])(
    'settles a missing status action id with an %s without publishing its shared tail',
    async (_label, status, exitCode, staleText) => {
      const actionId = 'a'.repeat(32)
      api.updateHermes.mockResolvedValue({ action_id: actionId, name: 'hermes-update', ok: true, pid: 1 })
      api.getActionStatus.mockResolvedValue(status)

      const button = await renderSystemView()
      vi.useFakeTimers()
      fireEvent.click(button)
      await act(async () => {
        await Promise.resolve()
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1200)
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1200)
      })

      expect(activity.upsertDesktopActionTask).toHaveBeenCalledTimes(1)
      expect(activity.upsertDesktopActionTask).toHaveBeenCalledWith({
        action_id: actionId,
        exit_code: exitCode,
        lines: [],
        name: 'hermes-update',
        pid: null,
        running: false
      })
      expect(JSON.stringify(activity.upsertDesktopActionTask.mock.calls)).not.toContain(staleText)
      expect(api.getActionStatus).toHaveBeenCalledTimes(1)
    }
  )

  it('retains the legacy pre-action-ID compatibility path', async () => {
    const status = {
      exit_code: 0,
      lines: ['legacy generation complete'],
      name: 'hermes-update',
      pid: null,
      running: false
    }

    api.updateHermes.mockResolvedValue({ name: 'hermes-update', ok: true, pid: 1 })
    api.getActionStatus.mockResolvedValue(status)

    const button = await renderSystemView()
    vi.useFakeTimers()
    fireEvent.click(button)
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200)
    })

    expect(activity.upsertDesktopActionTask).toHaveBeenCalledWith(status)
  })
})
