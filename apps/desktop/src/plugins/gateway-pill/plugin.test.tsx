import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  gateway: 'closed',
  reconnectGateway: vi.fn<() => Promise<void>>(),
  notifyError: vi.fn()
}))

vi.mock('@hermes/plugin-sdk', () => ({
  atom: (initial: unknown) => {
    let value = initial

    return {
      get: () => value,
      listen: vi.fn(),
      set: (next: unknown) => {
        value = next
      }
    }
  },
  Button: ({ children, ...props }: React.ComponentProps<'button'>) => <button {...props}>{children}</button>,
  cn: (...values: Array<false | null | string | undefined>) => values.filter(Boolean).join(' '),
  evaluateRuntimeReadiness: vi.fn().mockResolvedValue({ ready: true }),
  host: {
    logs: vi.fn().mockResolvedValue({ lines: [] }),
    navigate: vi.fn(),
    notifyError: mocks.notifyError,
    reconnectGateway: mocks.reconnectGateway,
    request: vi.fn(),
    restartGateway: vi.fn().mockResolvedValue(undefined),
    state: {
      gateway: {
        get: () => mocks.gateway,
        listen: vi.fn()
      }
    },
    status: vi.fn().mockResolvedValue({ gateway_platforms: {} })
  },
  icons: {
    Activity: (props: React.ComponentProps<'svg'>) => <svg {...props} />,
    AlertCircle: (props: React.ComponentProps<'svg'>) => <svg {...props} />,
    LayoutDashboard: (props: React.ComponentProps<'svg'>) => <svg {...props} />,
    RefreshCw: (props: React.ComponentProps<'svg'>) => <svg {...props} />
  },
  LogView: ({ children }: { children: React.ReactNode }) => <pre>{children}</pre>,
  StatusDot: () => <span />,
  Tip: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useI18n: () => ({
    t: {
      commandCenter: { restartGateway: 'Restart gateway' },
      shell: {
        gatewayMenu: {
          checkingInference: 'Checking inference',
          connected: 'Connected',
          connecting: 'Connecting',
          disconnected: 'Disconnected',
          inferenceNotReady: 'Inference not ready',
          inferenceReady: 'Inference ready',
          messagingPlatforms: 'Messaging platforms',
          offline: 'Offline',
          openSystem: 'Open system panel',
          recentActivity: 'Recent activity',
          reconnectGateway: 'Reconnect gateway',
          viewAllLogs: 'View all logs'
        },
        statusbar: {}
      }
    }
  }),
  useValue: (store: { get: () => unknown }) => store.get()
}))

import { GatewayMenuPanel } from './plugin'

describe('GatewayMenuPanel reconnect action', () => {
  beforeEach(() => {
    mocks.gateway = 'closed'
    mocks.reconnectGateway.mockReset().mockResolvedValue(undefined)
    mocks.notifyError.mockReset()
  })

  afterEach(() => {
    vi.clearAllTimers()
  })

  it('shows reconnect only while disconnected and triggers one reconnect at a time', async () => {
    let finish: (() => void) | undefined
    mocks.reconnectGateway.mockImplementation(
      () =>
        new Promise<void>(resolve => {
          finish = resolve
        })
    )

    render(<GatewayMenuPanel onClose={vi.fn()} />)
    await act(async () => undefined)

    const reconnect = screen.getByRole('button', { name: 'Reconnect gateway' })
    fireEvent.click(reconnect)
    fireEvent.click(reconnect)

    expect(mocks.reconnectGateway).toHaveBeenCalledOnce()
    expect((reconnect as HTMLButtonElement).disabled).toBe(true)

    await act(async () => finish?.())
  })

  it('hides reconnect while the socket is open', async () => {
    mocks.gateway = 'open'

    render(<GatewayMenuPanel onClose={vi.fn()} />)
    await act(async () => undefined)

    expect(screen.queryByRole('button', { name: 'Reconnect gateway' })).toBeNull()
  })
})
