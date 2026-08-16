import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type * as Nanostores from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { deleteProfile } from '@/hermes'
import { refreshPrimaryProfile, refreshProfiles, selectProfile, setActiveProfile, switchProfile } from '@/store/profile'
import type { ProfileInfo } from '@/types/hermes'

import { ProfilesView } from './index'

// These tests pin the invariant this whole area exists to hold: the Manage
// Profiles page and the sidebar rail share ONE set of profile dialogs, so both
// "New Profile" entry points render the same modal (SOUL.md included), and
// deleting the profile the gateway is on re-homes to default instead of
// stranding it on a dead backend. The drift that motivated the fix got in
// precisely because nothing rendered this view.

afterEach(cleanup)

// Real i18n (useI18n falls back to English with no provider), so labels are the
// actual strings — no brittle key snapshot to maintain here.

// CodeEditor is CodeMirror; the detail pane's SOUL editor doesn't matter to
// these behaviors, so stub it out of the jsdom render.
vi.mock('@/components/chat/code-editor', () => ({
  CodeEditor: () => null
}))

vi.mock('@/hermes', () => ({
  createProfile: vi.fn(async () => ({ name: 'x', ok: true, path: '/x' })),
  deleteProfile: vi.fn(async () => ({ ok: true, path: '/x' })),
  getProfileSoul: vi.fn(async () => ({ content: '', exists: true })),
  renameProfile: vi.fn(async () => ({ name: 'x', ok: true, path: '/x' })),
  updateProfileSoul: vi.fn(async () => ({ ok: true }))
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

const {
  $activeGatewayProfile: activeGateway,
  $activeProfile: activePrimary,
  $primaryDesktopProfileState: primaryDesktopState,
  $profileColors
} = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof Nanostores

  return {
    $activeGatewayProfile: atom<string>('default'),
    $activeProfile: atom<string>('default'),
    $primaryDesktopProfileState: atom({ current: 'default', persisted: 'default' as null | string | undefined }),
    $profileColors: atom<Record<string, string>>({})
  }
})

vi.mock('@/store/profile', () => ({
  $activeGatewayProfile: activeGateway,
  $activeProfile: activePrimary,
  $primaryDesktopProfileState: primaryDesktopState,
  $profileColors,
  normalizeProfileKey: (name: null | string | undefined) => (name ?? '').trim() || 'default',
  refreshPrimaryProfile: vi.fn(async () => undefined),
  refreshProfiles: vi.fn(async () => [] as ProfileInfo[]),
  selectProfile: vi.fn(),
  setActiveProfile: vi.fn(),
  switchProfile: vi.fn()
}))

// The one non-default profile these tests act on. Its name doubles as the row's
// accessible name, so the delete helper queries by it rather than a literal.
const NAMED_PROFILE = 'work'

beforeEach(() => {
  activePrimary.set('default')
  primaryDesktopState.set({ current: 'default', persisted: 'default' })
  vi.mocked(refreshPrimaryProfile).mockReset()
  vi.mocked(refreshPrimaryProfile).mockResolvedValue(primaryDesktopState.get())
})

function makeProfile(name: string, isDefault = false): ProfileInfo {
  return {
    has_env: false,
    is_default: isDefault,
    model: null,
    name,
    path: `/home/user/.hermes/profiles/${name}`,
    provider: null,
    skill_count: 0
  }
}

// Radix's trigger opens on the pointerdown/up pair, not the synthetic click
// alone — fire the full sequence a real click produces.
function realClick(el: HTMLElement) {
  fireEvent.pointerDown(el, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(el, { button: 0, pointerType: 'mouse' })
  fireEvent.click(el)
}

// ProfilesView loads its list in a mount effect (refreshProfiles → setProfiles),
// so the first paint is the loader and the rows commit a microtask later. Flush
// that inside act() so the rows exist before anything queries them, and so the
// mount setState isn't left unwrapped.
async function renderProfilesView() {
  await act(async () => {
    render(<ProfilesView onClose={vi.fn()} />)
  })
}

// PanelListRow labels BOTH the row's select target and its kebab with the
// profile name (`menuLabel={profile.name}`), so the name alone matches two
// buttons. Only the kebab is a menu trigger, so `expanded` disambiguates.
function findRowMenu(profileName: string) {
  return screen.findByRole('button', { expanded: false, name: profileName })
}

// Open the (only non-default) row's actions menu → Delete → confirm. The
// confirm click kicks off an async chain (deleteProfile → onDeleted refresh →
// setProfiles, plus the re-home writes), so settle it inside act() to flush
// those updates deterministically instead of leaking them past the assertions.
async function deleteTheNamedProfile() {
  realClick(await findRowMenu(NAMED_PROFILE))
  fireEvent.click(await screen.findByRole('menuitem', { name: /delete/i }))
  const confirm = await screen.findByRole('button', { name: 'Delete' })
  await act(async () => {
    fireEvent.click(confirm)
  })
}

describe('ProfilesView', () => {
  it('opens the shared create dialog with the SOUL.md field (parity with the rail)', async () => {
    vi.mocked(refreshProfiles).mockResolvedValue([])

    await renderProfilesView()

    realClick(await screen.findByRole('button', { name: 'New profile' }))

    const soul = await screen.findByLabelText(/SOUL\.md/i)

    expect(soul.tagName).toBe('TEXTAREA')
    expect(soul.getAttribute('id')).toBe('new-profile-soul')
  })

  it('re-homes to default when the active profile is deleted', async () => {
    vi.mocked(refreshProfiles).mockResolvedValue([makeProfile('default', true), makeProfile(NAMED_PROFILE)])
    activeGateway.set(NAMED_PROFILE)

    await renderProfilesView()
    await deleteTheNamedProfile()

    await waitFor(() => expect(deleteProfile).toHaveBeenCalledWith(NAMED_PROFILE))
    await waitFor(() => expect(selectProfile).toHaveBeenCalledWith('default'))
    expect(setActiveProfile).toHaveBeenCalledWith('default')
  })

  it('leaves the active profile alone when a different profile is deleted', async () => {
    vi.mocked(selectProfile).mockClear()
    vi.mocked(setActiveProfile).mockClear()
    vi.mocked(refreshProfiles).mockResolvedValue([makeProfile('default', true), makeProfile(NAMED_PROFILE)])
    activeGateway.set('default')

    await renderProfilesView()
    await deleteTheNamedProfile()

    await waitFor(() => expect(deleteProfile).toHaveBeenCalledWith(NAMED_PROFILE))
    // The dialog closes once the delete settles; a non-active delete must not re-home.
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull())
    expect(selectProfile).not.toHaveBeenCalled()
    expect(setActiveProfile).not.toHaveBeenCalled()
  })

  it('offers an explicit action that switches the primary Desktop profile back to default', async () => {
    vi.mocked(refreshProfiles).mockResolvedValue([makeProfile('default', true), makeProfile(NAMED_PROFILE)])
    vi.mocked(refreshPrimaryProfile).mockImplementationOnce(async () => {
      activePrimary.set(NAMED_PROFILE)
      const state = { current: NAMED_PROFILE, persisted: NAMED_PROFILE }
      primaryDesktopState.set(state)

      return state
    })

    await renderProfilesView()
    expect(refreshPrimaryProfile).toHaveBeenCalledOnce()
    realClick(await findRowMenu('default'))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Make primary Desktop profile' }))

    await waitFor(() => expect(switchProfile).toHaveBeenCalledWith('default'))
  })

  it('supports making a named profile primary', async () => {
    vi.mocked(switchProfile).mockClear()
    vi.mocked(refreshProfiles).mockResolvedValue([makeProfile('default', true), makeProfile(NAMED_PROFILE)])
    activePrimary.set('default')

    await renderProfilesView()
    realClick(await findRowMenu(NAMED_PROFILE))
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Make primary Desktop profile' }))

    await waitFor(() => expect(switchProfile).toHaveBeenCalledWith(NAMED_PROFILE))
  })

  it('marks the current primary Desktop profile and disables its restart action', async () => {
    vi.mocked(switchProfile).mockClear()
    vi.mocked(refreshProfiles).mockResolvedValue([makeProfile('default', true), makeProfile(NAMED_PROFILE)])
    activePrimary.set('default')

    await renderProfilesView()
    expect(await screen.findByText('Primary for Desktop')).toBeTruthy()
    realClick(await findRowMenu('default'))
    const action = await screen.findByRole('menuitem', { name: 'Make primary Desktop profile' })

    expect(action.getAttribute('data-disabled')).not.toBeNull()
    fireEvent.click(action)
    expect(switchProfile).not.toHaveBeenCalled()
  })
})
