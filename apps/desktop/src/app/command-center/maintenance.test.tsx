import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import type { ActionResponse, ActionStatusResponse } from '@/types/hermes'

import { MaintenancePanel } from './maintenance'

// Mirrors the constants in maintenance.tsx — the poll cadence and the ceiling
// after which the panel stops tailing a still-running action.
const ACTION_POLL_MS = 1200
const ACTION_POLL_LIMIT = 240

// Every spawn-backed op button is gated on the last observed action status.
const OP_BUTTONS = ['Run doctor', 'Security audit', 'Create backup', 'Run now']

const getActionStatus = vi.fn<(name: string, lines?: number) => Promise<ActionStatusResponse>>()
const getCuratorStatus = vi.fn()
const getMemoryStatus = vi.fn()
const startAction = vi.fn<() => Promise<ActionResponse>>()

vi.mock('@/hermes', () => ({
  getActionStatus: (name: string, lines?: number) => getActionStatus(name, lines),
  getCuratorStatus: () => getCuratorStatus(),
  getMemoryStatus: () => getMemoryStatus(),
  resetMemory: () => Promise.resolve({ deleted: [] }),
  runBackup: () => startAction(),
  runCurator: () => startAction(),
  runDebugShare: () => Promise.resolve({ urls: {} }),
  runDoctor: () => startAction(),
  runSecurityAudit: () => startAction(),
  setCuratorPaused: () => Promise.resolve()
}))

vi.mock('@/store/activity', () => ({ upsertDesktopActionTask: () => {} }))
vi.mock('@/store/notifications', () => ({ notify: () => {}, notifyError: () => {} }))

const TAILED_LINE = 'doctor: checking providers'

function actionStatus(overrides: Partial<ActionStatusResponse> = {}): ActionStatusResponse {
  return { exit_code: null, lines: [TAILED_LINE], name: 'doctor', pid: 4242, running: true, ...overrides }
}

function opButton(name: string): HTMLButtonElement {
  return screen.getByRole('button', { name }) as HTMLButtonElement
}

function opButtonsDisabled(): boolean[] {
  return OP_BUTTONS.map(name => opButton(name).disabled)
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

/** Render the panel and press "Run doctor", settling on the first polled status. */
async function launchDoctor() {
  render(
    <I18nProvider configClient={null} initialLocale="en">
      <MaintenancePanel />
    </I18nProvider>
  )

  await waitFor(() => expect(opButton('Run now')).toBeTruthy())

  fireEvent.click(opButton('Run doctor'))

  await waitFor(() => expect(opButtonsDisabled()).toEqual([true, true, true, true]))
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  startAction.mockResolvedValue({ name: 'doctor', ok: true, pid: 4242 })
  getCuratorStatus.mockResolvedValue({
    archive_after_days: null,
    enabled: true,
    interval_hours: 6,
    last_run_at: null,
    min_idle_hours: null,
    paused: false,
    stale_after_days: null
  })
  getMemoryStatus.mockResolvedValue({ active: '', builtin_files: { memory: 0, user: 0 }, providers: [] })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.resetAllMocks()
})

describe('MaintenancePanel action gate', () => {
  it('releases the op buttons when the status endpoint starts failing mid-tail', async () => {
    getActionStatus.mockResolvedValueOnce(actionStatus()).mockRejectedValue(new Error('status endpoint unreachable'))

    await launchDoctor()

    await advance(ACTION_POLL_MS)

    await waitFor(() => expect(opButtonsDisabled()).toEqual([false, false, false, false]))

    // The tail we already collected must survive the settle — only `running`
    // changes, so the user keeps the log they were reading.
    expect(screen.getByText(TAILED_LINE)).toBeTruthy()
    expect(screen.queryByText('Running...')).toBeNull()
  })

  it('releases the op buttons when the poll ceiling is reached on a long-running action', async () => {
    getActionStatus.mockResolvedValue(actionStatus())

    await launchDoctor()

    // One poll short of the ceiling the action is still being tailed, so the
    // gate must stay closed.
    await advance((ACTION_POLL_LIMIT - 2) * ACTION_POLL_MS)

    expect(opButtonsDisabled()).toEqual([true, true, true, true])

    await advance(2 * ACTION_POLL_MS)

    await waitFor(() => expect(opButtonsDisabled()).toEqual([false, false, false, false]))
    expect(screen.getByText(TAILED_LINE)).toBeTruthy()
  })

  it('leaves the normal completion path untouched', async () => {
    getActionStatus
      .mockResolvedValueOnce(actionStatus())
      .mockResolvedValue(actionStatus({ exit_code: 0, lines: [TAILED_LINE, 'doctor: ok'], running: false }))

    await launchDoctor()

    await advance(ACTION_POLL_MS)

    await waitFor(() => expect(opButtonsDisabled()).toEqual([false, false, false, false]))
    expect(screen.getByText(/doctor: ok/)).toBeTruthy()

    // A finished action stops the tail: no further polls are scheduled.
    expect(getActionStatus).toHaveBeenCalledTimes(2)

    await advance(10 * ACTION_POLL_MS)

    expect(getActionStatus).toHaveBeenCalledTimes(2)
  })
})
