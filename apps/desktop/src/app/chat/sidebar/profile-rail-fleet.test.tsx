import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopAgentRoster, DesktopConnectionsRegistry } from '@/global'
import { $notifications, clearNotifications } from '@/store/notifications'

import { ProfileRail } from './profile-switcher'

// The fleet rail: with several registered gateways, every gateway's agents sit
// on the one strip — the active gateway's squares exactly as before, the rest
// as at-rest groups behind a hairline + kind glyph. Clicking an at-rest square
// performs the same re-home the statusbar switcher does, on that exact
// (gateway, profile). Single-gateway rendering must stay byte-identical.

const navigate = vi.fn()
const selectConnection = vi.fn()
const selectProfile = vi.fn()
const getAgentRoster = vi.fn()
const cancelPrewarm = vi.fn()
const startPrewarm = vi.fn()

vi.mock('react-router', () => ({
  useNavigate: () => navigate
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel', delete: 'Delete' },
      profiles: {
        actions: 'Actions',
        allProfiles: 'All profiles',
        autoColor: 'Auto',
        color: 'Color…',
        colorFor: 'Color',
        connectGateway: 'Manage gateways…',
        editSoul: 'Edit SOUL.md…',
        exportProfile: 'Export profile…',
        failedLoadSoul: 'Failed to load SOUL.md',
        failedSaveSoul: 'Failed to save SOUL.md',
        fleet: {
          allOnGateway: 'All profiles on this gateway',
          consequence: 'Sessions moves to this gateway; the open chat stays on its current machine.',
          deleteOn: (gateway: string) => ` on ${gateway}`,
          gateway: (gateway: string) => `Profiles on ${gateway}`,
          gatewayUnreachable: (gateway: string) => `${gateway} · unreachable`,
          onGateway: (name: string, gateway: string) => `${name} · ${gateway}`,
          routeInvalid: (gateway: string) => `Can't switch to ${gateway}. The profile route is missing or ambiguous.`,
          unreachableTuple: (name: string, gateway: string) => `${name} · ${gateway} · unreachable`,
          authRequiredTuple: (name: string, gateway: string) => `${name} · ${gateway} · sign-in required`,
          switchFailed: (name: string, gateway: string, previous: string) =>
            `Could not switch to ${name} on ${gateway}. You’re still on ${previous}. Nothing was sent.`,
          switching: (name: string, gateway: string) => `Switching Sessions to ${name} on ${gateway}…`,
          switchTo: (name: string, gateway: string) => `Switch to ${name} on ${gateway}`
        },
        importProfile: 'Import profile…',
        manageProfiles: 'Manage profiles…',
        newProfile: 'New profile',
        remoteOverride: {
          badge: (host: string) => `Runs on ${host}`,
          menuItem: 'Connect to a remote host…'
        },
        renameMenu: 'Rename…',
        saveSoul: 'Save',
        saving: 'Saving…',
        setColor: (color: string) => `Set color ${color}`,
        showAllProfiles: 'Show all profiles',
        soulSaved: 'SOUL.md saved',
        switchConnectionFailed: (name: string) => `Could not connect to ${name}`,
        switchToProfile: (name: string) => `Switch to ${name}`,
        title: 'Profiles'
      },
      settings: { connections: { kindCloud: 'Cloud', kindLocal: 'This device', kindRemote: 'Remote', kindSsh: 'SSH' } }
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
  selectProfile: (name: string) => selectProfile(name),
  setProfileColor: vi.fn(),
  setProfileOrder: vi.fn(),
  setShowAllProfiles: vi.fn(),
  sortByProfileOrder: (profiles: unknown[]) => profiles
}))

vi.mock('@/store/connections', () => ({
  $activeConnectionId: atom<null | string>(null),
  $connectionsRegistry: atom<DesktopConnectionsRegistry | null>(null),
  $hasMultipleConnections: atom(false),
  selectConnection: (...args: unknown[]) => selectConnection(...args)
}))

vi.mock('@/store/profile-share', () => ({
  runExportProfileFlow: vi.fn(),
  runImportProfileFlow: vi.fn()
}))

vi.mock('./use-profile-prewarm', () => ({
  useProfilePrewarm: () => ({ cancelPrewarm, startPrewarm })
}))

vi.mock('./use-profile-rail-refresh-on-active', () => ({
  useProfileRailRefreshOnActive: () => undefined
}))

vi.mock('@/hermes', () => ({
  getProfileSoul: vi.fn().mockResolvedValue({ content: '' }),
  updateProfileSoul: vi.fn()
}))

vi.mock('@/components/chat/code-editor', () => ({ CodeEditor: () => null }))
vi.mock('../../profiles/create-profile-dialog', () => ({ CreateProfileDialog: () => null }))
vi.mock('../../profiles/delete-profile-dialog', () => ({ DeleteProfileDialog: () => null }))
vi.mock('../../profiles/rename-profile-dialog', () => ({ RenameProfileDialog: () => null }))

const connectionsStore = await import('@/store/connections')
const hasMultipleConnections = connectionsStore.$hasMultipleConnections as ReturnType<typeof atom<boolean>>
const activeConnectionId = connectionsStore.$activeConnectionId as ReturnType<typeof atom<null | string>>

const connectionsRegistry = connectionsStore.$connectionsRegistry as ReturnType<
  typeof atom<DesktopConnectionsRegistry | null>
>

const { $profiles, $profileScope, $activeGatewayProfile } = await import('@/store/profile')
const profiles = $profiles as ReturnType<typeof atom<Array<{ is_default: boolean; name: string }>>>
const profileScope = $profileScope as ReturnType<typeof atom<string>>
const gatewayProfile = $activeGatewayProfile as ReturnType<typeof atom<string>>
const { _resetFleetRosterForTests } = await import('@/store/fleet-roster')

const registry: DesktopConnectionsRegistry = {
  connections: [
    { id: 'local', kind: 'local', label: 'This device' },
    { id: 'pandora', kind: 'remote', label: 'Pandora', url: 'https://pandora.example' },
    { id: 'vps', kind: 'ssh', label: 'VPS', host: 'vps.example' }
  ],
  launchMode: 'primary',
  lastUsed: 'pandora',
  primary: 'pandora',
  version: 2
} as DesktopConnectionsRegistry

const roster: DesktopAgentRoster = {
  agents: [
    {
      connectionId: 'pandora',
      connectionKind: 'remote',
      connectionLabel: 'Pandora',
      profile: 'default',
      handle: 'hermes-pandora'
    },
    {
      connectionId: 'pandora',
      connectionKind: 'remote',
      connectionLabel: 'Pandora',
      profile: 'scout',
      handle: 'scout'
    },
    {
      connectionId: 'local',
      connectionKind: 'local',
      connectionLabel: 'This device',
      profile: 'default',
      handle: 'hermes'
    },
    { connectionId: 'local', connectionKind: 'local', connectionLabel: 'This device', profile: 'omer', handle: 'omer' }
  ],
  sources: [
    { connectionId: 'pandora', kind: 'remote', label: 'Pandora', reachable: true },
    { connectionId: 'local', kind: 'local', label: 'This device', reachable: true },
    { connectionId: 'vps', kind: 'ssh', label: 'VPS', reachable: false, error: 'timed out' }
  ]
}

function armFleet() {
  hasMultipleConnections.set(true)
  connectionsRegistry.set(registry)
  activeConnectionId.set('pandora')
  profiles.set([
    { is_default: true, name: 'default' },
    { is_default: false, name: 'scout' }
  ])
}

async function renderFleet() {
  const view = render(<ProfileRail />)

  // The roster arrives asynchronously via the Electron bridge.
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })

  return view.container
}

beforeEach(() => {
  getAgentRoster.mockResolvedValue(roster)
  selectConnection.mockResolvedValue(undefined)
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = { getAgentRoster }
})

afterEach(() => {
  cleanup()
  clearNotifications()
  vi.clearAllMocks()
  _resetFleetRosterForTests()
  hasMultipleConnections.set(false)
  connectionsRegistry.set(null)
  activeConnectionId.set(null)
  profileScope.set('default')
  gatewayProfile.set('default')
  profiles.set([{ is_default: true, name: 'default' }])
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

describe('ProfileRail fleet mode', () => {
  it('stays on the single-gateway path with one registered gateway', async () => {
    const container = await renderFleet()

    expect(getAgentRoster).not.toHaveBeenCalled()
    expect(screen.queryByRole('group', { name: /^Profiles on/ })).toBeNull()
    expect(container.querySelector('[data-slot="profile-rail-divider"]')).toBeNull()
    expect(screen.getByRole('button', { name: 'Manage gateways…' })).toBeTruthy()
  })

  it('lays every other gateway on the strip as an at-rest group, in switcher order', async () => {
    armFleet()
    const container = await renderFleet()

    expect(getAgentRoster).toHaveBeenCalledTimes(1)

    const groups = Array.from(container.querySelectorAll('[data-slot="profile-rail-gateway"]')).map(node => [
      node.getAttribute('data-connection-id'),
      node.getAttribute('data-active') === 'true'
    ])

    // Registry order for the whole strip — This device first (switcher
    // order), then by label — with the active gateway (Pandora) in ITS slot,
    // never pulled to the front.
    expect(groups).toEqual([
      ['local', false],
      ['pandora', true],
      ['vps', false]
    ])

    // Every group is headed by its kind glyph; hairlines only between groups.
    const dividers = Array.from(container.querySelectorAll('[data-slot="profile-rail-divider"]')).map(node =>
      node.getAttribute('data-connection-id')
    )

    expect(dividers).toEqual(['local', 'pandora', 'vps'])

    const local = screen.getByRole('group', { name: 'Profiles on This device' })
    expect(within(local).getByRole('button', { name: 'Switch to default on This device' })).toBeTruthy()
    expect(within(local).getByRole('button', { name: 'Switch to omer on This device' })).toBeTruthy()

    // The active gateway's own squares are unchanged and unqualified.
    expect(screen.getByRole('button', { name: 'scout' })).toBeTruthy()
    expect(screen.getByRole('status', { name: 'default · Pandora' })).toBeTruthy()

    // Fleet pill: "all on this gateway" replaces the default↔all toggle.
    expect(screen.getByRole('button', { name: 'All profiles on this gateway' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Manage gateways…' })).toBeNull()
  })

  it('marks an unreachable gateway but never hides it', async () => {
    armFleet()
    const container = await renderFleet()

    const vps = container.querySelector('[data-slot="profile-rail-gateway"][data-connection-id="vps"]')
    expect(vps?.getAttribute('data-reachable')).toBe('false')
    expect(
      container.querySelector(
        '[data-slot="profile-rail-divider"][data-connection-id="vps"] [data-slot="profile-rail-unreachable"]'
      )
    ).toBeTruthy()
    expect(within(vps as HTMLElement).getByRole('button', { name: 'default · VPS · unreachable' })).toBeTruthy()
  })

  it('re-homes onto the exact (gateway, profile) when an at-rest square is clicked', async () => {
    armFleet()
    await renderFleet()

    let settle: () => void = () => undefined
    selectConnection.mockImplementationOnce(() => new Promise<void>(resolve => (settle = resolve)))

    const omer = screen.getByRole('button', { name: 'Switch to omer on This device' })
    fireEvent.click(omer)

    expect(selectConnection).toHaveBeenCalledWith('local', { profile: 'omer' })
    expect(selectProfile).not.toHaveBeenCalled()
    // The dial spinner sits on the clicked square, not in the statusbar.
    expect(omer.getAttribute('aria-busy')).toBe('true')

    await act(async () => {
      settle()
      await Promise.resolve()
    })

    expect(omer.getAttribute('aria-busy')).toBeNull()
  })

  it('re-homes onto another gateway default from its home square', async () => {
    armFleet()
    await renderFleet()

    fireEvent.click(screen.getByRole('button', { name: 'Switch to default on This device' }))

    expect(selectConnection).toHaveBeenCalledWith('local', { profile: 'default' })
  })

  it('keeps the active gateway click on the plain profile path', async () => {
    armFleet()
    await renderFleet()

    fireEvent.click(screen.getByRole('button', { name: 'scout' }))

    expect(selectProfile).toHaveBeenCalledWith('scout')
    expect(selectConnection).not.toHaveBeenCalled()
  })

  it('does not prewarm active-gateway profiles while the fleet rail is visible', async () => {
    armFleet()
    await renderFleet()

    fireEvent.pointerEnter(screen.getByRole('button', { name: 'scout' }))

    expect(startPrewarm).not.toHaveBeenCalled()
    expect(cancelPrewarm).not.toHaveBeenCalled()
  })

  it('keeps every group in its slot when a different gateway is active', async () => {
    armFleet()
    activeConnectionId.set('local')
    profiles.set([
      { is_default: true, name: 'default' },
      { is_default: false, name: 'omer' }
    ])
    const container = await renderFleet()

    const groups = Array.from(container.querySelectorAll('[data-slot="profile-rail-gateway"]')).map(node => [
      node.getAttribute('data-connection-id'),
      node.getAttribute('data-active') === 'true'
    ])

    expect(groups).toEqual([
      ['local', true],
      ['pandora', false],
      ['vps', false]
    ])
    expect(screen.getByRole('button', { name: 'Switch to scout on Pandora' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'omer' })).toBeTruthy()
  })

  it('counts the whole fleet toward the condensed threshold and sections the menu by gateway', async () => {
    armFleet()
    profiles.set([
      { is_default: true, name: 'default' },
      ...Array.from({ length: 11 }, (_, index) => ({ is_default: false, name: `p${index + 1}` }))
    ])
    // 11 named on Pandora + local (default, omer) + vps (default) = 14 > 13.
    const container = await renderFleet()

    expect(screen.getByRole('button', { name: 'Profiles' })).toBeTruthy()
    expect(container.querySelector('[data-slot="profile-rail-rest-square"]')).toBeNull()
  })

  it('treats the active exact tuple as status rather than a second action', async () => {
    armFleet()
    await renderFleet()

    expect(screen.getByRole('status', { name: 'default · Pandora' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'default' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Switch to default on Pandora' })).toBeNull()
  })

  it('labels other fleet actions with the exact tuple and explains the Sessions move', async () => {
    armFleet()
    await renderFleet()

    const omer = screen.getByRole('button', { name: 'Switch to omer on This device' })
    expect(omer.getAttribute('aria-description')).toBe(
      'Sessions moves to this gateway; the open chat stays on its current machine.'
    )
    expect(screen.queryByRole('button', { name: /Open Bot Chat/i })).toBeNull()
    expect(screen.queryByText('Open Bot Chat')).toBeNull()
  })

  it('announces pending and failure in a polite live region without moving focus', async () => {
    armFleet()
    await renderFleet()

    let rejectSwitch: (error: Error) => void = () => undefined
    selectConnection.mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectSwitch = reject
        })
    )

    const omer = screen.getByRole('button', { name: 'Switch to omer on This device' })
    omer.focus()
    fireEvent.click(omer)

    const live = screen.getByRole('status', { name: /Switching Sessions to omer on This device/ })
    expect(live.getAttribute('aria-live')).toBe('polite')
    expect(globalThis.document.activeElement).toBe(omer)

    await act(async () => {
      rejectSwitch(new Error('dial failed'))
      await Promise.resolve()
    })

    expect(
      screen.getByRole('status', {
        name: 'Could not switch to omer on This device. You’re still on default · Pandora. Nothing was sent.'
      })
    ).toBeTruthy()
    expect($notifications.get()).toContainEqual(
      expect.objectContaining({
        title: 'Could not switch to omer on This device. You’re still on default · Pandora. Nothing was sent.'
      })
    )
    expect(globalThis.document.activeElement).toBe(omer)
  })

  it('moves focus to the named active tuple after a successful gateway switch', async () => {
    armFleet()
    await renderFleet()

    selectConnection.mockImplementationOnce(async () => {
      activeConnectionId.set('local')
      gatewayProfile.set('omer')
      profiles.set([
        { is_default: true, name: 'default' },
        { is_default: false, name: 'omer' }
      ])
    })

    const omer = screen.getByRole('button', { name: 'Switch to omer on This device' })
    omer.focus()
    fireEvent.click(omer)

    await act(async () => {
      await Promise.resolve()
    })

    expect(globalThis.document.activeElement).toBe(screen.getByRole('status', { name: 'omer · This device' }))
  })

  it('moves focus to the default active tuple after a successful gateway switch', async () => {
    armFleet()
    await renderFleet()

    selectConnection.mockImplementationOnce(async () => {
      activeConnectionId.set('local')
      gatewayProfile.set('default')
      profiles.set([
        { is_default: true, name: 'default' },
        { is_default: false, name: 'omer' }
      ])
    })

    const home = screen.getByRole('button', { name: 'Switch to default on This device' })
    home.focus()
    fireEvent.click(home)

    await act(async () => {
      await Promise.resolve()
    })

    expect(globalThis.document.activeElement).toBe(screen.getByRole('status', { name: 'default · This device' }))
  })

  it('does not steal focus when the user moves to the composer while a gateway switch is pending', async () => {
    armFleet()
    await renderFleet()

    let settleSwitch: () => void = () => undefined
    selectConnection.mockImplementationOnce(
      () =>
        new Promise<void>(resolve => {
          settleSwitch = () => {
            activeConnectionId.set('local')
            gatewayProfile.set('omer')
            profiles.set([
              { is_default: true, name: 'default' },
              { is_default: false, name: 'omer' }
            ])
            resolve()
          }
        })
    )

    fireEvent.click(screen.getByRole('button', { name: 'Switch to omer on This device' }))

    const composer = globalThis.document.createElement('textarea')
    globalThis.document.body.append(composer)
    composer.focus()

    await act(async () => {
      settleSwitch()
      await Promise.resolve()
    })

    expect(globalThis.document.activeElement).toBe(composer)
    composer.remove()
  })

  it('treats a named active exact tuple as status, including mac-cockpit', async () => {
    armFleet()
    activeConnectionId.set('local')
    gatewayProfile.set('mac-cockpit')
    profiles.set([
      { is_default: true, name: 'default' },
      { is_default: false, name: 'mac-cockpit' }
    ])
    await renderFleet()

    expect(screen.getByRole('status', { name: 'mac-cockpit · This device' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'mac-cockpit' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Switch to default on This device' })).toBeTruthy()
  })

  it('ignores a second rest-square click while a switch is pending and restores on failure', async () => {
    armFleet()
    await renderFleet()

    let rejectFirst: (error: Error) => void = () => undefined
    selectConnection.mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectFirst = reject
        })
    )

    fireEvent.click(screen.getByRole('button', { name: 'Switch to omer on This device' }))
    fireEvent.click(screen.getByRole('button', { name: 'default · VPS · unreachable' }))

    expect(selectConnection).toHaveBeenCalledTimes(1)
    expect(selectConnection).toHaveBeenCalledWith('local', { profile: 'omer' })

    await act(async () => {
      rejectFirst(new Error('dial failed'))
      await Promise.resolve()
    })

    expect(activeConnectionId.get()).toBe('pandora')

    fireEvent.click(screen.getByRole('button', { name: 'default · VPS · unreachable' }))
    expect(selectConnection).toHaveBeenCalledWith('vps', { profile: 'default' })
  })

  it('shows profile plus connection on condensed trigger and rest rows', async () => {
    armFleet()
    profiles.set([
      { is_default: true, name: 'default' },
      ...Array.from({ length: 11 }, (_, index) => ({ is_default: false, name: `p${index + 1}` }))
    ])
    await renderFleet()

    const trigger = screen.getByRole('button', { name: 'Profiles' })
    expect(trigger.textContent).toContain('default · Pandora')

    fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
    fireEvent.click(trigger)

    expect(await screen.findByText('omer · This device')).toBeTruthy()
    expect(screen.getByText('default · VPS · unreachable')).toBeTruthy()
  })

  it('keeps the fleet live region sr-only so the icon rail has no visible prose', async () => {
    armFleet()
    await renderFleet()

    let settle: () => void = () => undefined
    selectConnection.mockImplementationOnce(() => new Promise<void>(resolve => (settle = resolve)))

    fireEvent.click(screen.getByRole('button', { name: 'Switch to omer on This device' }))

    const live = screen.getByRole('status', { name: /Switching Sessions to omer on This device/ })
    expect(live.className).toContain('sr-only')
    expect(live.getAttribute('role')).toBe('status')
    expect(live.getAttribute('aria-live')).toBe('polite')

    await act(async () => {
      settle()
      await Promise.resolve()
    })
  })

  it('names unreachable routes with the tuple and explicit text, never color only', async () => {
    armFleet()
    await renderFleet()

    const vps = screen.getByRole('button', { name: /default · VPS · unreachable/ })
    expect(vps.textContent || vps.getAttribute('aria-label')).toMatch(/unreachable/)
  })
})
