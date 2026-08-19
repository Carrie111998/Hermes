import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Pins the rail's click contract against the
// REAL topology this feature exists for: a remote primary that serves all of
// its named profiles through one shared socket ("Mecha Hermes (HP)") plus a
// second registered gateway with its own `default` ("MechaHome Hermes (Dell)").
//
// The bug these cover: a rail square that goes dead — no highlight, no rescope
// — because $activeGatewayProfile never moved. Every one of those failures is
// an activation that got rejected or skipped, so the assertions all read the
// same atom the rail's `active` prop and $profileScope derive from.

const gatewayMocks = vi.hoisted(() => ({
  connect: vi.fn(async (_wsUrl: string): Promise<void> => undefined),
  setConnection: vi.fn(),
  setGatewayState: vi.fn()
}))

vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => []),
  setApiRequestConnection: vi.fn(),
  setApiRequestProfile: vi.fn(),
  STARTUP_REQUEST_TIMEOUT_MS: 1000,
  HermesGateway: class {
    connectionState = 'closed'
    connect = async (wsUrl: string): Promise<void> => {
      await gatewayMocks.connect(wsUrl)
      this.connectionState = 'open'
    }
    close = (): void => {
      this.connectionState = 'closed'
    }
    onEvent = vi.fn(() => () => {})
    onState = vi.fn(() => () => {})
  }
}))
vi.mock('@/store/session', () => ({
  setConnection: gatewayMocks.setConnection,
  setGatewayState: gatewayMocks.setGatewayState,
  // A gateway hop re-seeds the workspace from the machine it landed on.
  $activeSessionId: atom<null | string>(null),
  setCurrentBranch: vi.fn(),
  setCurrentCwdTransient: vi.fn()
}))
vi.mock('@/lib/desktop-fs', () => ({
  desktopDefaultCwd: vi.fn(async () => ({ branch: 'main', cwd: '/home/dell/workspace' }))
}))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline: vi.fn() }))
vi.mock('@/lib/query-client', () => ({ invalidateProfileScopedQueries: vi.fn() }))
vi.mock('@/store/cron-model-impact-scope', () => ({ invalidateCronModelImpactScopeState: vi.fn() }))
vi.mock('@/store/starmap', () => ({ resetStarmapGraph: vi.fn() }))

const { closeSecondaryGateways, configureGatewayRegistry, setPrimaryGateway } = await import('./gateway')
const { $activeGatewayProfile, ensureGatewayProfile, normalizeProfileKey, selectProfile } = await import('./profile')

const {
  $attachedConnectionId,
  $foreignAgents,
  $primaryConnectionId,
  refreshGatewaySeparation,
  selectForeignAgent,
  selectPrimaryAgent
} = await import('./gateway-separation')

const HP = 'hp'
const DELL = 'dell'

const HP_URL = 'http://192.168.1.218:9119'
const DELL_URL = 'http://192.168.1.204:9120'

const hpDescriptor = (profile: string) => ({
  authMode: 'token',
  baseUrl: HP_URL,
  mode: 'remote',
  // The HP primary is a REMOTE gateway, so every one of its named profiles
  // routes THROUGH the primary socket and getConnection tags the descriptor
  // `sharedPrimary` (electron/main.ts ensureBackend, routing case 3). This is
  // the shape the real setup hands the renderer — not a per-profile socket.
  profile,
  sharedPrimary: true,
  token: 'fake-test-token',
  wsUrl: `ws://192.168.1.218:9119/api/ws?token=fake-test-token`
})

const dellDescriptor = {
  authMode: 'token',
  baseUrl: DELL_URL,
  mode: 'remote',
  profile: 'default',
  token: 'fake-test-token',
  wsUrl: 'ws://192.168.1.204:9120/api/ws?token=fake-test-token'
}

const REGISTRY = {
  primary: HP,
  connections: [
    { id: HP, kind: 'remote', label: 'Mecha Hermes (HP)', url: HP_URL },
    { id: DELL, kind: 'remote', label: 'MechaHome Hermes (Dell)', url: DELL_URL }
  ]
}

const ROSTER = {
  sources: [
    { connectionId: HP, reachable: true },
    { connectionId: DELL, reachable: true }
  ],
  agents: [
    ...['default', 'appdev', 'codertom', 'makebofagent', 'menuscript'].map(profile => ({
      connectionId: HP,
      connectionLabel: 'Mecha Hermes (HP)',
      handle: profile,
      profile
    })),
    { connectionId: DELL, connectionLabel: 'MechaHome Hermes (Dell)', handle: 'default', profile: 'default' }
  ]
}

function installDesktop(): void {
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {
    connections: { list: vi.fn(async () => REGISTRY) },
    getAgentRoster: vi.fn(async () => ROSTER),
    getConnection: vi.fn(async (profile?: string) => hpDescriptor(normalizeProfileKey(profile))),
    // Descriptors are per (connection, profile) — the mock has to answer for
    // BOTH machines, or a test cannot tell a trip home from staying put.
    getConnectionFor: vi.fn(async ({ connectionId, profile }: { connectionId: string; profile?: string }) =>
      connectionId === DELL ? dellDescriptor : hpDescriptor(normalizeProfileKey(profile))
    )
  }
}

/** Boot the rail's world: primary socket open on HP `default`, the registry
 *  mirror wired exactly as use-gateway-boot wires it (the mirror is what turns
 *  a rejected activation into a dead square, so a test without it cannot see
 *  the bug). */
async function boot(): Promise<void> {
  installDesktop()
  configureGatewayRegistry({
    onEvent: vi.fn(),
    onActiveRouteChanged: profile => {
      const key = normalizeProfileKey(profile)

      if (normalizeProfileKey($activeGatewayProfile.get()) !== key) {
        $activeGatewayProfile.set(key)
      }
    }
  })
  setPrimaryGateway({ connectionState: 'open' } as never, 'default')
  $activeGatewayProfile.set('default')
  await refreshGatewaySeparation()
  // Attachment is DERIVED, never written by the rail: the primary socket going
  // live leaves no registry connection active, so $attachedConnectionId falls
  // back to the primary — exactly as it does on a real boot.
}

beforeEach(async () => {
  await boot()
})

afterEach(() => {
  closeSecondaryGateways()
  vi.clearAllMocks()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

/** selectPrimaryAgent is fire-and-forget (one activation per click, started
 *  and not awaited), so a test has to let the switch settle before reading. */
const settle = () => new Promise(resolve => setTimeout(resolve, 0))

/** Click the Dell square: the live gateway moves to the other machine, and its
 *  profile is ALSO called `default`, which is what makes the trip home hard. */
const hopToDell = () =>
  selectForeignAgent({
    connectionId: DELL,
    connectionLabel: 'MechaHome Hermes (Dell)',
    handle: 'default',
    profile: 'default',
    reachable: true
  })

/** Click one of the HP's squares while the live gateway is on the Dell. From
 *  there the primary's agents are FOREIGN squares — the rail's own squares are
 *  whatever machine it is attached to — so the trip home goes through the same
 *  door the trip out did, just pointed the other way. */
const hopHome = (profile: string) =>
  selectForeignAgent({
    connectionId: HP,
    connectionLabel: 'Mecha Hermes (HP)',
    handle: profile,
    profile,
    reachable: true
  })

describe('rail squares on the primary', () => {
  it('seeds attachment to the registry primary', () => {
    expect($primaryConnectionId.get()).toBe(HP)
    expect($attachedConnectionId.get()).toBe(HP)
  })

  it('moves the active profile when a named square is clicked', async () => {
    selectPrimaryAgent('appdev')
    await settle()

    expect($activeGatewayProfile.get()).toBe('appdev')
    expect($attachedConnectionId.get()).toBe(HP)
  })

  it('moves between two named squares', async () => {
    selectPrimaryAgent('appdev')
    await settle()
    selectPrimaryAgent('codertom')
    await settle()

    expect($activeGatewayProfile.get()).toBe('codertom')
  })

  it('returns to default from a named square', async () => {
    selectPrimaryAgent('menuscript')
    await settle()
    selectPrimaryAgent('default')
    await settle()

    expect($activeGatewayProfile.get()).toBe('default')
  })

  it('survives two clicks landing in the same tick (impatient double-click)', async () => {
    selectPrimaryAgent('appdev')
    selectPrimaryAgent('appdev')
    await settle()

    expect($activeGatewayProfile.get()).toBe('appdev')
  })
})

describe('rail squares after a cross-gateway hop', () => {
  it('comes back to a NAMED primary profile from the Dell agent', async () => {
    await hopToDell()
    expect($attachedConnectionId.get()).toBe(DELL)

    selectPrimaryAgent('appdev')
    await settle()

    expect($activeGatewayProfile.get()).toBe('appdev')
    expect($attachedConnectionId.get()).toBe(HP)
  })

  it('comes back to the primary DEFAULT square from the Dell agent', async () => {
    await hopToDell()

    // Both machines call this profile `default`, so $activeGatewayProfile —
    // which publishes the BARE profile name — still reads 'default' while the
    // live socket is on the Dell. Nothing keyed on that name can tell this is a
    // real switch, which is why the trip home cannot go through the name-keyed
    // door: `ensureGatewayProfile('default')` sees "already on default" and
    // returns. Addressing the agent by (connection, profile) is what makes the
    // same-named square on the other machine reachable at all.
    await hopHome('default')
    await settle()

    expect($attachedConnectionId.get()).toBe(HP)
    expect(gatewayMocks.setConnection).toHaveBeenCalledWith(expect.objectContaining({ baseUrl: HP_URL }))
  })

  it("sends the primary's own descriptor, not the Dell's, when a named square comes home", async () => {
    await hopToDell()
    selectPrimaryAgent('appdev')
    await settle()

    expect(gatewayMocks.setConnection).toHaveBeenLastCalledWith(
      expect.objectContaining({ baseUrl: HP_URL, profile: 'appdev' })
    )
  })
})

describe('rail split (the rail may not draw an agent twice)', () => {
  const keys = () => $foreignAgents.get().map(a => `${a.connectionId}:${a.profile}`)

  it("treats the other machine's agents as foreign while attached to the primary", () => {
    expect(keys()).toEqual([`${DELL}:default`])
  })

  it('re-derives the split the moment the live gateway lands on the Dell', async () => {
    await hopToDell()

    // The rail's own squares are $profiles, which follows the AMBIENT
    // connection — so once the hop lands they describe the Dell. Keeping the
    // split pinned to the primary instead drew the Dell's `default` twice:
    // once as a native square, once as a foreign one.
    expect(keys()).not.toContain(`${DELL}:default`)
    expect(keys()).toContain(`${HP}:default`)
    expect(keys()).toHaveLength(5)
  })

  it('puts the split back when the trip home actually lands', async () => {
    await hopToDell()
    expect(keys()).toContain(`${HP}:default`)

    await hopHome('default')
    await settle()

    // Deliberately NOT synchronous with the click. Writing the attachment
    // optimistically also moved the atom the sidebar reseeds on, so the session
    // refetch went out while the API layer's ambient connection was still the
    // Dell — it re-fetched the Dell's rows and relabelled them as the primary's.
    // The split follows the activation that actually landed.
    expect(keys()).toEqual([`${DELL}:default`])
  })
})

describe('a gateway hop re-points machine-scoped state', () => {
  it('re-seeds the workspace from the machine it landed on', async () => {
    const { desktopDefaultCwd } = await import('@/lib/desktop-fs')
    const { setCurrentCwdTransient } = await import('@/store/session')

    await hopToDell()
    await settle()

    // A cwd is a path on ONE box. Carrying the primary's across the hop asked
    // the Dell for a directory it does not have, and the workspace panel
    // rendered "Could not read this folder (ENOENT)".
    expect(desktopDefaultCwd).toHaveBeenCalled()
    expect(setCurrentCwdTransient).toHaveBeenCalledWith('/home/dell/workspace')
  })

  it('leaves the workspace alone while a session is open', async () => {
    const { $activeSessionId, setCurrentCwdTransient } = await import('@/store/session')

    // Let the boot's own re-seed land first, so this asserts on the hop alone.
    await settle()
    vi.mocked(setCurrentCwdTransient).mockClear()
    $activeSessionId.set('live-session')

    try {
      await hopToDell()
      await settle()

      // An open session owns the cwd.
      expect(setCurrentCwdTransient).not.toHaveBeenCalled()
    } finally {
      $activeSessionId.set(null)
    }
  })

  it('never rejects when the new machine cannot answer', async () => {
    const { desktopDefaultCwd } = await import('@/lib/desktop-fs')
    vi.mocked(desktopDefaultCwd).mockRejectedValueOnce(new Error('unreachable'))

    // The switch itself already landed; a failed re-seed must not surface as an
    // unhandled rejection on top of a successful hop.
    await expect(hopToDell()).resolves.toBeUndefined()
    await settle()
  })
})

describe('one activation per click', () => {
  it('a second concurrent activation for the same target does not strand the first', async () => {
    // The shape the earlier selectPrimaryAgent had: selectProfile starts one
    // activation internally, and awaiting a second on top of it raced the
    // epoch. Pinned here so re-adding the "surface failures" await fails loudly
    // instead of silently killing the square.
    selectProfile('codertom')
    await ensureGatewayProfile('codertom')
    await settle()

    expect($activeGatewayProfile.get()).toBe('codertom')
  })
})
