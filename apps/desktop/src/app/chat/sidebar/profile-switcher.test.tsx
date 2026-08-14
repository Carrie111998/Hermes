import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import {
  $activeGatewayProfile,
  $profileColors,
  $profileOrder,
  $profiles,
  setShowAllProfiles
} from '@/store/profile'
import type * as WindowsStoreModule from '@/store/windows'
import type { ProfileInfo } from '@/types/hermes'

import { ProfileRail } from './profile-switcher'

const openNewWindow = vi.hoisted(() => vi.fn())

vi.mock('@/store/windows', async importOriginal => ({
  ...(await importOriginal<typeof WindowsStoreModule>()),
  openNewWindow: (profile?: string) => openNewWindow(profile)
}))
vi.mock('@/store/profile-share', () => ({
  runExportProfileFlow: vi.fn(),
  runImportProfileFlow: vi.fn()
}))
vi.mock('./use-profile-prewarm', () => ({
  useProfilePrewarm: () => ({ cancelPrewarm: vi.fn(), startPrewarm: vi.fn() })
}))
vi.mock('../../profiles/create-profile-dialog', () => ({ CreateProfileDialog: () => null }))
vi.mock('../../profiles/delete-profile-dialog', () => ({ DeleteProfileDialog: () => null }))
vi.mock('../../profiles/rename-profile-dialog', () => ({ RenameProfileDialog: () => null }))

const profiles: ProfileInfo[] = [
  {
    has_env: false,
    is_default: true,
    model: null,
    name: 'default',
    path: '/tmp/default',
    provider: null,
    skill_count: 0
  },
  {
    has_env: false,
    is_default: false,
    model: null,
    name: 'work',
    path: '/tmp/work',
    provider: null,
    skill_count: 0
  }
]

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

function openContextMenu(target: HTMLElement) {
  fireEvent.pointerDown(target, { button: 2, ctrlKey: false, pointerType: 'mouse' })
  fireEvent.contextMenu(target, { button: 2 })
}

beforeEach(() => {
  openNewWindow.mockClear()
  $profiles.set(profiles)
  $profileOrder.set([])
  $profileColors.set({})
  $activeGatewayProfile.set('default')
  setShowAllProfiles(false)
  desktopWindow.hermesDesktop = {
    api: vi.fn(async ({ path }) =>
      path === '/api/profiles/active' ? { active: 'default', current: 'default' } : { profiles }
    )
  } as unknown as Window['hermesDesktop']
})

afterEach(() => {
  cleanup()

  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('ProfileRail profile context menu', () => {
  it('opens the selected profile in a new window from the first menu item', async () => {
    render(
      <MemoryRouter>
        <I18nProvider configClient={null} initialLocale="en">
          <ProfileRail />
        </I18nProvider>
      </MemoryRouter>
    )

    openContextMenu(screen.getByRole('button', { name: 'work' }))

    const items = await screen.findAllByRole('menuitem')
    const newWindow = items[0]

    expect(newWindow.textContent).toContain('New Window')
    expect(newWindow.querySelector('.codicon-multiple-windows')).not.toBeNull()

    fireEvent.click(newWindow)

    expect(openNewWindow).toHaveBeenCalledWith('work')
  })
})
