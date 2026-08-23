// @vitest-environment jsdom
import { cleanup, fireEvent, render, within } from '@testing-library/react'
import type * as Nanostores from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopUpdateStatus } from '@/global'

const mocks = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof Nanostores
  const idleApply = {
    applying: false,
    command: null,
    error: null,
    log: [],
    message: '',
    percent: null,
    stage: 'idle' as const
  }

  return {
    $backendUpdateApply: atom(idleApply),
    $backendUpdateChecking: atom(false),
    $backendUpdateStatus: atom<DesktopUpdateStatus | null>(null),
    $connection: atom({ mode: 'remote' }),
    $desktopVersion: atom({ appVersion: '0.20.5', bundleOutOfSync: false }),
    $updateApply: atom(idleApply),
    $updateChecking: atom(false),
    $updateStatus: atom<DesktopUpdateStatus | null>(null),
    checkBackendUpdates: vi.fn(),
    checkUpdates: vi.fn(),
    openUpdateOverlayFor: vi.fn(),
    refreshDesktopVersion: vi.fn(),
    restartBackendGatewayForSkew: vi.fn(),
    startUpdateFor: vi.fn()
  }
})

vi.mock('@/store/session', () => ({ $connection: mocks.$connection }))

vi.mock('@/store/updates', () => ({
  $backendUpdateApply: mocks.$backendUpdateApply,
  $backendUpdateChecking: mocks.$backendUpdateChecking,
  $backendUpdateStatus: mocks.$backendUpdateStatus,
  $desktopVersion: mocks.$desktopVersion,
  $updateApply: mocks.$updateApply,
  $updateChecking: mocks.$updateChecking,
  $updateStatus: mocks.$updateStatus,
  checkBackendUpdates: mocks.checkBackendUpdates,
  checkUpdates: mocks.checkUpdates,
  openUpdateOverlayFor: mocks.openUpdateOverlayFor,
  refreshDesktopVersion: mocks.refreshDesktopVersion,
  restartBackendGatewayForSkew: mocks.restartBackendGatewayForSkew,
  startUpdateFor: mocks.startUpdateFor
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      settings: {
        about: {
          automaticUpdates: 'Automatic updates',
          automaticUpdatesDesc: 'Checks automatically.',
          backendUpdates: 'Connected backend',
          branchCommit: (branch: string, commit: string) => `Branch ${branch} · Commit ${commit}`,
          bundleOutOfSync: 'App build out of date',
          bundleOutOfSyncAction: 'Get the installer',
          bundleOutOfSyncDesc: 'Install the latest desktop build.',
          cantReach: "We couldn't reach the update server.",
          cantUpdate: "This build can't update itself.",
          checkNow: 'Check now',
          checking: 'Checking…',
          clientUpdates: 'Desktop app',
          daysAgo: (count: number) => `${count} days ago`,
          heading: 'Hermes Desktop',
          hoursAgo: (count: number) => `${count} hours ago`,
          installing: 'An update is currently installing.',
          gatewayRestartRequired: 'The gateway is running older code and must restart.',
          justNow: 'just now',
          justNowSuffix: ' · just now',
          lastChecked: (age: string) => `Last checked ${age}`,
          minAgo: (count: number) => `${count} min ago`,
          never: 'never',
          onLatest: "You're on the latest version.",
          releaseNotes: 'Release notes',
          restartGateway: 'Restart gateway',
          seeWhatsNew: "See what's new",
          tapCheck: 'Tap Check now.',
          updateNow: 'Update now',
          updateReady: (count: number) => `A new update is ready (${count}).`,
          updateReadyUnknown: 'A new update is ready.',
          updates: 'Updates',
          version: (value: string) => `Version ${value}`,
          versionUnavailable: 'Version unavailable'
        }
      }
    }
  })
}))

vi.mock('@/components/brand-mark', () => ({ BrandMark: () => <div data-testid="brand-mark" /> }))
vi.mock('./uninstall-section', () => ({ UninstallSection: () => null }))

import { AboutSettings } from './about-settings'

const availableStatus = (targetSha: string): DesktopUpdateStatus => ({
  behind: 2,
  branch: 'main',
  currentSha: '0123456789abcdef',
  fetchedAt: Date.now(),
  supported: true,
  targetSha,
  updateAvailable: true
})

beforeEach(() => {
  mocks.$connection.set({ mode: 'remote' })
  mocks.$updateStatus.set(availableStatus('client-sha'))
  mocks.$backendUpdateStatus.set(availableStatus('backend-sha'))
  mocks.checkUpdates.mockResolvedValue(availableStatus('client-sha'))
  mocks.checkBackendUpdates.mockResolvedValue(availableStatus('backend-sha'))
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AboutSettings update targets', () => {
  it('pins every remote-mode row action to its displayed target', () => {
    const { container } = render(<AboutSettings />)
    const client = container.querySelector<HTMLElement>('[data-update-target="client"]')
    const backend = container.querySelector<HTMLElement>('[data-update-target="backend"]')

    expect(client).not.toBeNull()
    expect(backend).not.toBeNull()
    expect(within(client!).getByText('Desktop app')).toBeTruthy()
    expect(within(backend!).getByText('Connected backend')).toBeTruthy()

    fireEvent.click(within(client!).getByRole('button', { name: 'Check now' }))
    fireEvent.click(within(backend!).getByRole('button', { name: 'Check now' }))
    fireEvent.click(within(client!).getByRole('button', { name: 'Update now' }))
    fireEvent.click(within(backend!).getByRole('button', { name: 'Update now' }))
    fireEvent.click(within(client!).getByRole('button', { name: "See what's new" }))
    fireEvent.click(within(backend!).getByRole('button', { name: "See what's new" }))

    expect(mocks.checkUpdates).toHaveBeenCalledOnce()
    expect(mocks.checkBackendUpdates).toHaveBeenCalledOnce()
    expect(mocks.startUpdateFor.mock.calls).toEqual([['client'], ['backend']])
    expect(mocks.openUpdateOverlayFor.mock.calls).toEqual([['client'], ['backend']])
  })

  it('does not invent a backend row for a local connection', () => {
    mocks.$connection.set({ mode: 'local' })

    const { container } = render(<AboutSettings />)

    expect(container.querySelector('[data-update-target="client"]')).not.toBeNull()
    expect(container.querySelector('[data-update-target="backend"]')).toBeNull()
  })

  it('offers the skewed backend a pinned gateway restart action even when the checkout is current', () => {
    mocks.$backendUpdateStatus.set({
      ...availableStatus('backend-sha'),
      behind: 0,
      gatewayProfile: 'work',
      gatewayRestartRequired: true,
      updateAvailable: false
    })

    const { container } = render(<AboutSettings />)
    const backend = container.querySelector<HTMLElement>('[data-update-target="backend"]')

    expect(within(backend!).getByText('The gateway is running older code and must restart.')).toBeTruthy()
    fireEvent.click(within(backend!).getByRole('button', { name: 'Restart gateway' }))
    expect(mocks.restartBackendGatewayForSkew).toHaveBeenCalledOnce()
    expect(mocks.startUpdateFor).not.toHaveBeenCalled()
  })
})
