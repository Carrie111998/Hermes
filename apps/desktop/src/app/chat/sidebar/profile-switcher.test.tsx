import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import type { ProfileInfo } from '@/types/hermes'

const getConnectionConfig = vi.fn()
const selectBackend = vi.fn()
const prewarmBackend = vi.fn()

const $profiles = atom<ProfileInfo[]>([])
const $profileScope = atom('default')
const $activeGatewayProfile = atom('default')
const $profileOrder = atom<string[]>([])
const $profileColors = atom<Record<string, string>>({})
const $profileCreateRequest = atom(0)
const $connection = atom<HermesConnection | null>(null)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel', delete: 'Delete' },
      profiles: {
        actions: 'Actions',
        allProfiles: 'All profiles',
        autoColor: 'Auto',
        backendLocal: 'Mac backend',
        backendRemote: 'Remote backend',
        backendRemoteUnavailable: 'Set up a remote backend in Gateway settings first.',
        color: 'Color…',
        colorFor: 'Color',
        editSoul: 'Edit SOUL.md…',
        exportProfile: 'Export profile…',
        importProfile: 'Import profile…',
        manageProfiles: 'Manage profiles…',
        newProfile: 'New profile',
        renameMenu: 'Rename…',
        saveSoul: 'Save SOUL.md',
        saving: 'Saving...',
        setColor: (color: string) => `Set color ${color}`,
        showAllProfiles: 'Show all profiles',
        switchToProfile: (name: string) => `Switch to ${name}`,
        title: 'Profiles'
      }
    }
  })
}))

vi.mock('@/hermes', () => ({
  getProfileSoul: vi.fn(),
  updateProfileSoul: vi.fn()
}))

vi.mock('@/components/ui/tooltip', () => ({
  Tip: ({ children, label }: { children: ReactNode; label: ReactNode }) => (
    <span data-slot="tooltip-trigger" data-tip={typeof label === 'string' ? label : ''}>
      {children}
    </span>
  ),
  Tooltip: ({ children }: { children: ReactNode }) => <>{children}</>,
  TooltipContent: ({ children }: { children: ReactNode }) => <>{children}</>,
  TooltipProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
  TooltipTrigger: ({ children }: { children: ReactNode }) => <>{children}</>
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/session', () => ({
  $connection
}))

vi.mock('@/store/profile-share', () => ({
  runExportProfileFlow: vi.fn(),
  runImportProfileFlow: vi.fn()
}))

vi.mock('@/store/profile', () => ({
  $activeGatewayProfile,
  $profileColors,
  $profileCreateRequest,
  $profileOrder,
  $profiles,
  $profileScope,
  ALL_PROFILES: '__all__',
  normalizeProfileKey: (name: null | string | undefined) => {
    const value = (name ?? '').trim()

    return value || 'default'
  },
  prewarmBackend,
  refreshActiveProfile: vi.fn(),
  selectBackend,
  selectProfile: vi.fn(),
  setProfileColor: vi.fn(),
  setProfileOrder: vi.fn(),
  setShowAllProfiles: vi.fn(),
  sortByProfileOrder: <T extends { name: string }>(items: T[]) => items
}))

vi.mock('../../profiles/create-profile-dialog', () => ({
  CreateProfileDialog: () => null
}))

vi.mock('../../profiles/delete-profile-dialog', () => ({
  DeleteProfileDialog: () => null
}))

vi.mock('../../profiles/rename-profile-dialog', () => ({
  RenameProfileDialog: () => null
}))

function localConnection(overrides: Partial<HermesConnection> = {}): HermesConnection {
  return { baseUrl: '', mode: 'local', profile: 'default', ...overrides } as HermesConnection
}

function remoteConnection(overrides: Partial<HermesConnection> = {}): HermesConnection {
  return { baseUrl: 'https://remote.example', mode: 'remote', profile: 'default', ...overrides } as HermesConnection
}

async function flushEffects(count = 4) {
  await act(async () => {
    for (let index = 0; index < count; index += 1) {
      await Promise.resolve()
    }
  })
}

async function renderRail() {
  const { ProfileRail } = await import('./profile-switcher')

  render(
    <MemoryRouter>
      <ProfileRail />
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  $profiles.set([
    {
      has_env: false,
      is_default: true,
      model: null,
      name: 'default',
      path: '/tmp/hermes/default',
      provider: null,
      skill_count: 0
    }
  ])
  $profileScope.set('default')
  $activeGatewayProfile.set('default')
  $profileOrder.set([])
  $profileColors.set({})
  $profileCreateRequest.set(0)
  $connection.set(localConnection())
  getConnectionConfig.mockResolvedValue({
    cloudOrg: '',
    envOverride: false,
    mode: 'remote',
    profile: null,
    remoteAuthMode: 'token',
    remoteOauthConnected: false,
    remoteTokenPreview: null,
    remoteTokenSet: true,
    remoteUrl: 'https://remote.example',
    sshHost: '',
    sshKeyPath: '',
    sshPort: null,
    sshRemoteHermesPath: '',
    sshRemoteProfile: '',
    sshUser: ''
  })
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { getConnectionConfig }
  })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('ProfileRail backend controls', () => {
  it('renders fixed Mac and Remote controls and routes clicks through selectBackend', async () => {
    await renderRail()
    await flushEffects()

    expect(screen.getByRole('button', { name: 'Mac backend' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Remote backend' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Mac backend' }))
    expect(selectBackend).toHaveBeenCalledWith('local')

    fireEvent.click(screen.getByRole('button', { name: 'Remote backend' }))
    expect(selectBackend).toHaveBeenCalledWith('remote')
  })

  it('reflects the active backend from connection mode', async () => {
    $connection.set(remoteConnection())

    await renderRail()
    await flushEffects()

    expect(screen.getByRole('button', { name: 'Mac backend' }).getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByRole('button', { name: 'Remote backend' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('does not mark Mac active before connection mode is known', async () => {
    $connection.set(null)

    await renderRail()
    await flushEffects()

    expect(screen.getByRole('button', { name: 'Mac backend' }).getAttribute('aria-pressed')).toBe('false')
    expect(screen.getByRole('button', { name: 'Remote backend' }).getAttribute('aria-pressed')).toBe('false')
  })

  it('prewarms the inactive backend on pointer hover', async () => {
    $connection.set(localConnection())

    await renderRail()
    await flushEffects()

    fireEvent.pointerOver(screen.getByRole('button', { name: 'Remote backend' }))
    expect(prewarmBackend).toHaveBeenCalledWith('remote')

    fireEvent.pointerOver(screen.getByRole('button', { name: 'Mac backend' }))
    expect(prewarmBackend).toHaveBeenCalledTimes(1)
  })

  it('keeps the unavailable remote control focusable, blocked, and explained when no saved remote is available', async () => {
    getConnectionConfig.mockResolvedValue({
      cloudOrg: '',
      envOverride: false,
      mode: 'local',
      profile: null,
      remoteAuthMode: 'token',
      remoteOauthConnected: false,
      remoteTokenPreview: null,
      remoteTokenSet: false,
      remoteUrl: '',
      sshHost: '',
      sshKeyPath: '',
      sshPort: null,
      sshRemoteHermesPath: '',
      sshRemoteProfile: '',
      sshUser: ''
    })

    await renderRail()
    await flushEffects()

    const remoteButton = screen.getByRole('button', { name: 'Remote backend' })
    expect((remoteButton as HTMLButtonElement).disabled).toBe(false)
    expect(remoteButton.getAttribute('aria-disabled')).toBe('true')

    remoteButton.focus()
    expect(document.activeElement).toBe(remoteButton)

    fireEvent.click(remoteButton)
    expect(selectBackend).not.toHaveBeenCalled()

    const trigger = remoteButton.closest('[data-slot="tooltip-trigger"]')
    expect(trigger).toBeTruthy()
    expect(trigger?.getAttribute('data-tip')).toBe('Set up a remote backend in Gateway settings first.')
  })
})
