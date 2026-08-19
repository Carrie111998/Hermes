import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import type * as Nanostores from 'nanostores'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { deleteProfile, getProfiles } from '@/hermes'
import { activeGatewayConnectionId, retireAgentGateways } from '@/store/gateway'
import { refreshProfiles, selectProfile, setActiveProfile } from '@/store/profile'
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
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  deleteProfile: vi.fn(async () => ({ ok: true, path: '/x' })),
  getProfileSoul: vi.fn(async () => ({ content: '', exists: true })),
  renameProfile: vi.fn(async () => ({ name: 'x', ok: true, path: '/x' })),
  updateProfileSoul: vi.fn(async () => ({ ok: true }))
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

vi.mock('@/store/gateway', () => ({
  // Owner-scoped retirement: the local-only seam would tear down a same-named
  // LOCAL profile when the row being deleted belongs to another machine.
  retireAgentGateways: vi.fn(),
  retireLocalProfileGateways: vi.fn(),
  // Manage Profiles now lists every registered gateway, so it pulls in
  // store/gateway-separation, which mirrors the live gateway's connection.
  $gateway: atom<unknown>(null),
  activeGatewayConnectionId: vi.fn(() => null)
}))

const { $activeGatewayProfile: activeGateway, $profileColors } = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof Nanostores

  return {
    $activeGatewayProfile: atom<string>('default'),
    $profileColors: atom<Record<string, string>>({})
  }
})

vi.mock('@/store/profile', () => ({
  $activeGatewayProfile: activeGateway,
  $profileColors,
  normalizeProfileKey: (name: null | string | undefined) => (name ?? '').trim() || 'default',
  profileLabel: (profile: { display_name?: string; name: string }) =>
    (profile.display_name ?? '').trim() || profile.name,
  refreshProfiles: vi.fn(async () => [] as ProfileInfo[]),
  selectProfile: vi.fn(),
  setActiveProfile: vi.fn()
}))

// The one non-default profile these tests act on. Its name doubles as the row's
// accessible name, so the delete helper queries by it rather than a literal.
const NAMED_PROFILE = 'work'

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
    const deleteProfileMock = vi.mocked(deleteProfile)
    const retireMock = vi.mocked(retireAgentGateways)

    deleteProfileMock.mockClear()
    retireMock.mockClear()
    vi.mocked(refreshProfiles).mockResolvedValue([makeProfile('default', true), makeProfile(NAMED_PROFILE)])
    activeGateway.set(NAMED_PROFILE)

    await renderProfilesView()
    await deleteTheNamedProfile()

    await waitFor(() => expect(deleteProfile).toHaveBeenCalledWith(NAMED_PROFILE))
    // Retirement is addressed to the OWNING agent. '' is "no override" — a
    // single-gateway install, where the wire call stays byte-identical.
    expect(retireAgentGateways).toHaveBeenCalledWith('', NAMED_PROFILE)
    expect(retireMock.mock.invocationCallOrder[0]).toBeLessThan(deleteProfileMock.mock.invocationCallOrder[0])
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
})


// ── ownership across machines ──────────────────────────────────────────────
// Both gateways serve `default` AND an overlapping named profile. Without that
// overlap a bare name is still unambiguous and none of this can go wrong.
const HP = '192-168-1-218-9119'
const DELL = 'mechahome-hermes-dell'

function installTwoGatewayRegistry() {
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    connections: {
      list: vi.fn(async () => ({
        primary: HP,
        connections: [
          { id: 'local', kind: 'local', label: 'This device' },
          { id: HP, kind: 'remote', label: 'Mecha Hermes (HP)' },
          { id: DELL, kind: 'remote', label: 'MechaHome Hermes (Dell)' }
        ]
      }))
    },
    getAgentRoster: vi.fn(async () => ({
      sources: [
        { connectionId: HP, reachable: true },
        { connectionId: DELL, reachable: true }
      ],
      agents: [
        { connectionId: HP, connectionLabel: 'Mecha Hermes (HP)', handle: 'work', profile: NAMED_PROFILE },
        { connectionId: DELL, connectionLabel: 'MechaHome Hermes (Dell)', handle: 'work-dell', profile: NAMED_PROFILE }
      ]
    }))
  }
}

describe('ProfilesView across registered gateways', () => {
  it('addresses the PRIMARY by its registry id while the live gateway is elsewhere', async () => {
    const { refreshGatewaySeparation } = await import('@/store/gateway-separation')

    installTwoGatewayRegistry()
    // The live gateway is the Dell — the ambient connection every un-scoped
    // request would follow.
    vi.mocked(activeGatewayConnectionId).mockReturnValue(DELL)
    vi.mocked(getProfiles).mockResolvedValue({ profiles: [makeProfile('default', true)] })

    await act(async () => {
      await refreshGatewaySeparation()
    })
    await renderProfilesView()

    const targets = vi.mocked(getProfiles).mock.calls.map(call => call[0])

    // The section labelled with the primary's name must READ the primary. An
    // omitted id means "ambient", which while attached to the Dell made the
    // primary-labelled section list the Dell's profiles instead.
    expect(targets).toContain(HP)
    expect(targets).toContain(DELL)
    expect(targets).not.toContain('')
    expect(targets).not.toContain(undefined)
  })

  it('never re-homes when the deleted profile is a DIFFERENT machine\'s same-named one', async () => {
    const { refreshGatewaySeparation } = await import('@/store/gateway-separation')

    installTwoGatewayRegistry()
    vi.mocked(activeGatewayConnectionId).mockReturnValue(HP)
    vi.mocked(selectProfile).mockClear()
    vi.mocked(setActiveProfile).mockClear()
    vi.mocked(retireAgentGateways).mockClear()
    // The live agent is work@HP. The row we delete is work@DELL — same name,
    // different machine.
    activeGateway.set(NAMED_PROFILE)
    vi.mocked(getProfiles).mockImplementation(async (connectionId?: null | string) =>
      connectionId === DELL ? { profiles: [makeProfile(NAMED_PROFILE)] } : { profiles: [makeProfile('default', true)] }
    )

    await act(async () => {
      await refreshGatewaySeparation()
    })
    await renderProfilesView()
    await deleteTheNamedProfile()

    await waitFor(() => expect(deleteProfile).toHaveBeenCalledWith(NAMED_PROFILE, DELL))

    // Retirement is scoped to the DELL's agent. Routing it through the
    // local-only seam would have torn down work@HP's sockets instead (#88638).
    expect(retireAgentGateways).toHaveBeenCalledWith(DELL, NAMED_PROFILE)

    // And the live route is untouched: deciding "was it active?" by profile
    // NAME alone reset an innocent machine's gateway and pill to `default`.
    expect(selectProfile).not.toHaveBeenCalled()
    expect(setActiveProfile).not.toHaveBeenCalled()
  })
})
