import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopUpdateStatus } from '@/global'
import { I18nProvider } from '@/i18n'
import { setConnection } from '@/store/session'
import type * as UpdatesStore from '@/store/updates'
import {
  $backendUpdateApply,
  $backendUpdateChecking,
  $backendUpdateStatus,
  $desktopVersion,
  $updateApply,
  $updateChecking,
  $updateStatus
} from '@/store/updates'

import { AboutSettings } from './about-settings'

const actions = vi.hoisted(() => ({
  checkBackendUpdates: vi.fn(),
  checkUpdates: vi.fn(),
  openUpdateOverlayFor: vi.fn(),
  refreshDesktopVersion: vi.fn(),
  startUpdateFor: vi.fn()
}))

vi.mock('@/store/updates', async importOriginal => {
  const actual = await importOriginal<typeof UpdatesStore>()

  return { ...actual, ...actions }
})

vi.mock('./uninstall-section', () => ({ UninstallSection: () => null }))

const IDLE = {
  applying: false,
  stage: 'idle' as const,
  message: '',
  percent: null,
  error: null,
  command: null,
  log: []
}

const status = (over: Partial<DesktopUpdateStatus> = {}): DesktopUpdateStatus => ({
  supported: true,
  behind: 0,
  fetchedAt: 1,
  ...over
})

function setMode(mode: 'local' | 'remote') {
  setConnection({
    baseUrl: 'http://box:9119',
    isFullscreen: false,
    logs: [],
    mode,
    nativeOverlayWidth: 0,
    token: 'token',
    windowButtonPosition: null,
    wsUrl: 'ws://box:9119'
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  actions.checkUpdates.mockResolvedValue(status())
  actions.checkBackendUpdates.mockResolvedValue(status())
  $desktopVersion.set(null)
  $updateStatus.set(null)
  $backendUpdateStatus.set(null)
  $updateApply.set(IDLE)
  $backendUpdateApply.set(IDLE)
  $updateChecking.set(false)
  $backendUpdateChecking.set(false)
})

afterEach(() => {
  cleanup()
  setMode('local')
})

describe('AboutSettings update targets', () => {
  it('renders separate remote client/backend cards and routes every action to its card target', async () => {
    setMode('remote')
    $updateStatus.set(status({ behind: 1 }))
    $backendUpdateStatus.set(status({ behind: 2, currentVersion: '0.16.0', updateAvailable: true }))

    render(<AboutSettings />)

    const client = screen.getByRole('group', { name: 'Client' })
    const backend = screen.getByRole('group', { name: 'Backend' })

    fireEvent.click(within(client).getByRole('button', { name: 'Check now' }))
    fireEvent.click(within(backend).getByRole('button', { name: 'Check now' }))
    await waitFor(() => {
      expect(actions.checkUpdates).toHaveBeenCalledTimes(1)
      expect(actions.checkBackendUpdates).toHaveBeenCalledTimes(1)
    })

    fireEvent.click(within(client).getByRole('button', { name: 'Update now' }))
    fireEvent.click(within(backend).getByRole('button', { name: 'Update now' }))
    expect(actions.startUpdateFor).toHaveBeenNthCalledWith(1, 'client')
    expect(actions.startUpdateFor).toHaveBeenNthCalledWith(2, 'backend')

    fireEvent.click(within(client).getByRole('button', { name: "See what's new" }))
    fireEvent.click(within(backend).getByRole('button', { name: "See what's new" }))
    expect(actions.openUpdateOverlayFor).toHaveBeenNthCalledWith(1, 'client')
    expect(actions.openUpdateOverlayFor).toHaveBeenNthCalledWith(2, 'backend')
  })

  it('preserves the single client update card in local mode', () => {
    setMode('local')
    $updateStatus.set(status({ behind: 3 }))

    render(<AboutSettings />)

    expect(screen.getAllByRole('button', { name: 'Check now' })).toHaveLength(1)
    expect(screen.queryByRole('group', { name: 'Backend' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Update now' }))
    expect(actions.startUpdateFor).toHaveBeenCalledWith('client')
  })

  it('uses localized primary failure statuses instead of raw backend English', () => {
    setMode('remote')
    $updateStatus.set(status({ supported: false, message: 'This build cannot update itself.' }))
    $backendUpdateStatus.set(
      status({ error: 'check-failed', message: "Couldn't reach the update source — try again later." })
    )

    render(
      <I18nProvider configClient={null} initialLocale="ja">
        <AboutSettings />
      </I18nProvider>
    )

    expect(
      within(screen.getByRole('group', { name: 'クライアント' })).getByText(
        'このビルドはアプリ内から更新できません。'
      )
    ).toBeTruthy()
    expect(
      within(screen.getByRole('group', { name: 'バックエンド' })).getByText(
        '更新サーバーに接続できませんでした。'
      )
    ).toBeTruthy()
    expect(screen.queryByText('This build cannot update itself.')).toBeNull()
    expect(screen.queryByText("Couldn't reach the update source — try again later.")).toBeNull()
  })
})
