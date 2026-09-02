import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  confirm: vi.fn(),
  runDebugShare: vi.fn()
}))

vi.mock('@/store/confirm', () => ({ confirm: mocks.confirm }))
vi.mock('@/store/notifications', () => ({ notify: vi.fn(), notifyError: vi.fn() }))
vi.mock('@/store/activity', () => ({ upsertDesktopActionTask: vi.fn() }))
vi.mock('@/hermes', () => ({
  getActionStatus: vi.fn(),
  getCuratorStatus: vi.fn(async () => ({ enabled: false, last_run_at: null, paused: false })),
  getMemoryStatus: vi.fn(async () => ({
    active: '',
    builtin_files: { memory: 0, user: 0 }
  })),
  resetMemory: vi.fn(),
  runBackup: vi.fn(),
  runCurator: vi.fn(),
  runDebugShare: mocks.runDebugShare,
  runDoctor: vi.fn(),
  runSecurityAudit: vi.fn(),
  setCuratorPaused: vi.fn()
}))

import { I18nProvider } from '@/i18n/context'

import { MaintenancePanel } from './maintenance'

async function renderPanel() {
  await act(async () => {
    render(
      <I18nProvider configClient={null}>
        <MaintenancePanel />
      </I18nProvider>
    )
  })
}

beforeEach(() => {
  mocks.confirm.mockReset()
  mocks.runDebugShare.mockReset()
  mocks.runDebugShare.mockResolvedValue({
    auto_delete_seconds: 21600,
    failures: [],
    redacted: true,
    urls: { Report: 'https://paste.rs/example' }
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('MaintenancePanel debug share consent', () => {
  it('does not upload when the user cancels', async () => {
    mocks.confirm.mockResolvedValue(false)
    await renderPanel()

    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Debug share' })))

    expect(mocks.confirm).toHaveBeenCalledTimes(1)
    expect(mocks.runDebugShare).not.toHaveBeenCalled()
  })

  it('uploads exactly once when the user confirms', async () => {
    mocks.confirm.mockResolvedValue(true)
    await renderPanel()

    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Debug share' })))

    expect(mocks.confirm).toHaveBeenCalledTimes(1)
    expect(mocks.runDebugShare).toHaveBeenCalledTimes(1)
  })
})
