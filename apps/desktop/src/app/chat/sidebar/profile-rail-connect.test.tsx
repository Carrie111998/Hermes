import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { mergeCompactProfileOrder, ProfileRail } from './profile-switcher'

// The rail's discoverability pills are navigation, not identity — assert the
// multi-gateway entry point deep-links to Settings → Connections instead of
// relying on someone finding the pane three levels into Settings (the exact
// gap reported against the multi-connection registry launch).

const navigate = vi.fn()

const { gatewayState, requestGateway } = vi.hoisted(() => ({
  gatewayState: { current: { id: 'pc' } },
  requestGateway: vi.fn()
}))

vi.mock('react-router', () => ({
  useNavigate: () => navigate
}))

vi.mock('@/app/gateway/hooks/use-gateway-request', () => ({
  useGatewayRequest: () => ({ gateway: gatewayState.current, requestGateway })
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel' },
      profiles: {
        allProfiles: 'All profiles',
        connectGateway: 'Manage gateways…',
        failedLoadSoul: 'Failed to load SOUL.md',
        failedSaveSoul: 'Failed to save SOUL.md',
        importProfile: 'Import profile…',
        manageProfiles: 'Manage profiles…',
        newProfile: 'New profile',
        remoteOverride: {
          badge: (host: string) => `Runs on ${host}`,
          menuItem: 'Connect to a remote host…'
        },
        saveSoul: 'Save',
        saving: 'Saving…',
        showAllProfiles: 'Show all profiles',
        soulSaved: 'SOUL.md saved',
        switchToProfile: (name: string) => `Switch to ${name}`,
        title: 'Profiles'
      }
    }
  })
}))

vi.mock('@/store/profile', () => ({
  $activeGatewayProfile: atom('default'),
  $profileColors: atom({}),
  $profileCreateRequest: atom(0),
  $profileOrder: atom([]),
  $profiles: atom([{ is_default: true, name: 'default' }]),
  $profileScope: atom('default'),
  ALL_PROFILES: '*',
  normalizeProfileKey: (name: string) => name,
  profileLabel: (profile: { display_name?: string; name: string }) =>
    (profile.display_name ?? '').trim() || profile.name,
  refreshActiveProfile: vi.fn().mockResolvedValue(undefined),
  selectProfile: vi.fn(),
  setProfileColor: vi.fn(),
  setProfileOrder: vi.fn(),
  setShowAllProfiles: vi.fn(),
  sortByProfileOrder: (profiles: unknown[]) => profiles
}))

vi.mock('@/store/connections', () => ({ $hasMultipleConnections: atom(false) }))

vi.mock('@/store/profile-share', () => ({
  runExportProfileFlow: vi.fn(),
  runImportProfileFlow: vi.fn()
}))

vi.mock('./use-profile-prewarm', () => ({
  useProfilePrewarm: () => ({ cancelPrewarm: vi.fn(), startPrewarm: vi.fn() })
}))

vi.mock('@/hermes', () => ({
  getProfileSoul: vi.fn().mockResolvedValue({ content: '' }),
  updateProfileSoul: vi.fn()
}))

vi.mock('@/components/chat/code-editor', () => ({ CodeEditor: () => null }))
vi.mock('../../profiles/create-profile-dialog', () => ({ CreateProfileDialog: () => null }))
vi.mock('../../profiles/delete-profile-dialog', () => ({ DeleteProfileDialog: () => null }))
vi.mock('../../profiles/rename-profile-dialog', () => ({ RenameProfileDialog: () => null }))

const { $hasMultipleConnections } = await import('@/store/connections')
const hasMultipleConnections = $hasMultipleConnections as ReturnType<typeof atom<boolean>>
const { $activeGatewayProfile, $profileScope, $profiles } = await import('@/store/profile')

const profiles = $profiles as ReturnType<
  typeof atom<Array<{ display_name?: string; is_default: boolean; name: string }>>
>

const profileScope = $profileScope as ReturnType<typeof atom<string>>
const activeGatewayProfile = $activeGatewayProfile as ReturnType<typeof atom<string>>

beforeEach(() => {
  gatewayState.current = { id: 'pc' }
  requestGateway.mockReset()
  requestGateway.mockResolvedValue({ found: false })
})

afterEach(() => {
  cleanup()
  hasMultipleConnections.set(false)
  profiles.set([{ is_default: true, name: 'default' }])
  profileScope.set('default')
  activeGatewayProfile.set('default')
})

describe('ProfileRail multi-gateway entry point', () => {
  it('reorders visible compact profiles without moving the hidden active profile', () => {
    expect(mergeCompactProfileOrder(['picasso', 'founder', 'writer'], ['picasso', 'writer'], 'writer', 'picasso')).toEqual([
      'writer',
      'founder',
      'picasso'
    ])
  })

  it('deep-links to the unified Settings → Gateways page from the rail', () => {
    render(<ProfileRail />)

    const pill = screen.getByRole('button', { name: 'Manage gateways…' })
    fireEvent.click(pill)

    expect(navigate).toHaveBeenCalledWith('/settings?tab=gateway')
  })

  it('keeps the entry point visible for single-profile users', () => {
    render(<ProfileRail />)

    // The whole point is first-run discoverability: the pill must not be
    // gated behind multiProfile the way the default↔all toggle is.
    expect(screen.getByRole('button', { name: 'Manage gateways…' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Manage profiles…' })).toBeTruthy()
  })

  it('keeps the active profile explicit when gateway identity moves to the statusbar', () => {
    hasMultipleConnections.set(true)
    render(<ProfileRail />)

    expect(screen.getByRole('button', { name: 'default' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Manage gateways…' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Manage profiles…' })).toBeTruthy()
  })

  it('shows the default owner name and initial instead of a generic home glyph', () => {
    profiles.set([
      { display_name: 'Clyde', is_default: true, name: 'default' },
      { is_default: false, name: 'picasso' }
    ])
    render(<ProfileRail />)

    const owner = screen.getByRole('button', { name: 'Clyde' })

    expect(within(owner).getByText('C')).toBeTruthy()
    expect(within(owner).getByText('Clyde')).toBeTruthy()
  })

  it('expands the active named profile and collapses the inactive owner to a circular initial', () => {
    activeGatewayProfile.set('picasso')
    profileScope.set('picasso')
    profiles.set([
      { display_name: 'Clyde', is_default: true, name: 'default' },
      { display_name: 'Picasso', is_default: false, name: 'picasso' },
      { display_name: 'Founder', is_default: false, name: 'founder' }
    ])
    render(<ProfileRail />)

    const active = screen.getByRole('button', { name: 'Picasso' })
    const owner = screen.getByRole('button', { name: 'Clyde' })
    const founder = screen.getByRole('button', { name: 'Founder' })

    expect(within(active).getByText('Picasso')).toBeTruthy()
    expect(active.getAttribute('aria-pressed')).toBe('true')
    expect(owner.querySelector('[data-slot="profile-owner-compact"]')).toBeTruthy()
    expect(within(owner).getByText('C')).toBeTruthy()
    expect(within(owner).queryByText('Clyde')).toBeNull()
    expect(within(founder).getByText('F')).toBeTruthy()
  })

  it('uses the default profile avatar when the gateway has one', async () => {
    requestGateway.mockResolvedValue({ data: 'data:image/png;base64,YXZhdGFy', found: true })
    profiles.set([
      { display_name: 'Clyde', is_default: true, name: 'default' },
      { is_default: false, name: 'picasso' }
    ])
    render(<ProfileRail />)

    await waitFor(() =>
      expect(requestGateway).toHaveBeenCalledWith('profiles.get_asset', { asset: 'avatar', name: 'default' })
    )

    const owner = screen.getByRole('button', { name: 'Clyde' })
    const avatar = owner.querySelector('img')

    expect(avatar?.getAttribute('src')).toBe('data:image/png;base64,YXZhdGFy')
    expect(within(owner).queryByText('C')).toBeNull()
  })

  it('does not render the previous gateway owner while the next avatar loads', async () => {
    requestGateway.mockResolvedValueOnce({ data: 'data:image/png;base64,cGM=', found: true })
    profiles.set([
      { display_name: 'Clyde', is_default: true, name: 'default' },
      { is_default: false, name: 'picasso' }
    ])
    const { rerender } = render(<ProfileRail />)
    const owner = screen.getByRole('button', { name: 'Clyde' })

    await waitFor(() => expect(owner.querySelector('img')).toBeTruthy())

    gatewayState.current = { id: 'forge' }
    requestGateway.mockImplementationOnce(() => new Promise(() => undefined))
    rerender(<ProfileRail />)

    const switchedOwner = screen.getByRole('button', { name: 'Clyde' })

    expect(switchedOwner.querySelector('img')).toBeNull()
    expect(within(switchedOwner).getByText('C')).toBeTruthy()
  })

  it('keeps the layers control in the all-profiles state', () => {
    profileScope.set('*')
    profiles.set([
      { display_name: 'Clyde', is_default: true, name: 'default' },
      { is_default: false, name: 'picasso' }
    ])
    render(<ProfileRail />)

    const allProfiles = screen.getByRole('button', { name: 'All profiles' })

    expect(allProfiles.querySelector('.codicon-layers')).toBeTruthy()
    expect(within(allProfiles).queryByText('Clyde')).toBeNull()
  })

  it('keeps thirteen profiles direct and condenses the fourteenth', () => {
    profiles.set([
      { is_default: true, name: 'default' },
      ...Array.from({ length: 12 }, (_, index) => ({ is_default: false, name: `Profile ${index + 1}` }))
    ])
    const { unmount } = render(<ProfileRail />)

    expect(screen.queryByRole('button', { name: 'Profiles' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Profile 12' })).toBeTruthy()
    unmount()

    profiles.set([
      { is_default: true, name: 'default' },
      ...Array.from({ length: 13 }, (_, index) => ({ is_default: false, name: `Profile ${index + 1}` }))
    ])
    render(<ProfileRail />)

    expect(screen.getByRole('button', { name: 'Profiles' })).toBeTruthy()
  })

  it('stays shrinkable with many profiles and multiple gateways', () => {
    hasMultipleConnections.set(true)
    profiles.set([
      { is_default: true, name: 'default' },
      ...Array.from({ length: 13 }, (_, index) => ({ is_default: false, name: `Profile ${index + 1}` }))
    ])
    render(<ProfileRail />)

    expect(screen.getByRole('group', { name: 'Profiles' }).className).toContain('min-w-0')
    expect(screen.getByRole('button', { name: 'Profiles' })).toBeTruthy()
  })
})
