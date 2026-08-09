import { afterEach, describe, expect, it, vi } from 'vitest'

const connect = vi.fn(async () => undefined)
const close = vi.fn(() => undefined)
const onEvent = vi.fn(() => () => undefined)
const onState = vi.fn(() => () => undefined)
const setGatewayState = vi.fn()
const markNativeNotifyBaseline = vi.fn()
const resolveGatewayWsUrl = vi.fn(async () => 'ws://synthetic-gateway.test/ws')

vi.mock('@hermes/shared', () => ({ resolveGatewayWsUrl }))
vi.mock('@/store/session', () => ({ setGatewayState }))
vi.mock('@/store/notify-baseline', () => ({ markNativeNotifyBaseline }))
vi.mock('@/lib/reconnect-backoff', () => ({ reconnectBackoffDelayMs: () => 1 }))
vi.mock('@/hermes', () => ({
  HermesGateway: vi.fn().mockImplementation(() => ({
    connect,
    close,
    connectionState: 'closed',
    onEvent,
    onState
  }))
}))

describe('gatewayTargetKey', () => {
  afterEach(async () => {
    const gateway = await import('./gateway')

    gateway.closeSecondaryGateways()
    gateway.setPrimaryGateway(null)
    gateway.setPrimaryBackendMode('local')
    vi.resetModules()
    vi.clearAllMocks()
  })

  it('keeps local and remote roots distinct when both profiles are default', async () => {
    const { gatewayTargetKey } = await import('./gateway')

    expect(gatewayTargetKey('default', { localOnly: true })).toBe('__local__:default')
    expect(gatewayTargetKey('default', { remoteOnly: true })).toBe('__remote__:default')
    expect(gatewayTargetKey('default')).toBe('default')
  })
})
