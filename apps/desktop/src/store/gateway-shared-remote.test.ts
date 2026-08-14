import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The global-remote share (backend routing case 3): every profile is served
// by the PRIMARY backend over one host, and ensureBackend tags that
// descriptor with `sharedPrimary: true`. Dialing a second WebSocket at that
// descriptor used to fail over SSH (per-backend tunnel/ticket) and poison the
// active gateway with a closed socket — "Hermes gateway is not connected" for
// every profile except the primary. Pooled backends (local `--profile serve`
// or per-profile remote overrides) also carry a `profile` name but must dial
// their own socket — checking `.profile` alone misclassified them (#85745).

vi.mock('@/hermes', () => ({
  HermesGateway: class {
    connectionState = 'closed'
    connect = vi.fn(async () => {
      throw new Error('dialed a socket for a shared-primary profile')
    })
    onEvent = vi.fn(() => () => {})
    onState = vi.fn(() => () => {})
  }
}))
vi.mock('@/store/session', () => ({ setGatewayState: vi.fn() }))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline: vi.fn() }))

const { $gateway, configureGatewayRegistry, ensureGatewayForProfile, setPrimaryGateway } = await import('./gateway')

type DesktopStub = { getConnection: ReturnType<typeof vi.fn> }

function installDesktop(stub: DesktopStub): void {
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = stub
}

function makePrimary(): { connectionState: string } {
  // Only connectionState is consulted by setActive/isOpen for these paths.
  return { connectionState: 'open' }
}

beforeEach(() => {
  configureGatewayRegistry({
    onEvent: vi.fn(),
    primaryProfile: 'default'
  } as never)
})

afterEach(() => {
  vi.clearAllMocks()
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('ensureGatewayForProfile under a shared global remote', () => {
  it('activates the primary socket for a shared-primary descriptor', async () => {
    const primary = makePrimary()
    setPrimaryGateway(primary as never, 'default')
    installDesktop({
      getConnection: vi.fn(async () => ({
        port: 4242,
        profile: 'venture',
        sharedPrimary: true,
        token: 't'
      }))
    })

    await ensureGatewayForProfile('venture')

    expect($gateway.get()).toBe(primary)
  })

  it('pools a socket for a local profile descriptor tagged with profile but not sharedPrimary', async () => {
    const primary = makePrimary()
    setPrimaryGateway(primary as never, 'default')
    installDesktop({
      getConnection: vi.fn(async () => ({
        port: 5151,
        profile: 'bubu',
        token: 't2',
        wsUrl: 'ws://127.0.0.1:5151/api/ws?token=t2'
      }))
    })

    await ensureGatewayForProfile('bubu')

    expect($gateway.get()).not.toBe(primary)
  })

  it('still pools a socket for profiles with their own untagged descriptor', async () => {
    const primary = makePrimary()
    setPrimaryGateway(primary as never, 'default')
    installDesktop({
      getConnection: vi.fn(async () => ({ port: 5151, token: 't2' }))
    })

    await ensureGatewayForProfile('worker')

    expect($gateway.get()).not.toBe(primary)
  })
})
