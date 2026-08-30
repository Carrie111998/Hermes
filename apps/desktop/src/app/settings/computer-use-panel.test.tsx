import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ComputerUseStatus } from '@/types/hermes'

const getActionStatus = vi.fn()
const getComputerUseStatus = vi.fn()
const grantComputerUsePermissions = vi.fn()

vi.mock('@/hermes', () => ({
  getActionStatus: (...args: unknown[]) => getActionStatus(...args),
  getComputerUseStatus: (...args: unknown[]) => getComputerUseStatus(...args),
  grantComputerUsePermissions: (...args: unknown[]) => grantComputerUsePermissions(...args)
}))

vi.mock('@/store/activity', () => ({
  upsertDesktopActionTask: vi.fn()
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: vi.fn()
}))

const { resetComputerUseGrantLedger } = await import('./computer-use-grants')

const scope = { connectionId: 'homelab', profile: 'inbox-bot' }
const otherScope = { connectionId: 'workstation', profile: 'coder' }

const status: ComputerUseStatus = {
  accessibility: false,
  can_grant: true,
  checks: [],
  error: null,
  installed: true,
  platform: 'darwin',
  platform_supported: true,
  ready: false,
  screen_recording: false,
  screen_recording_capturable: false,
  source: null,
  version: 'cua-driver 0.22.1'
}

beforeEach(() => {
  getComputerUseStatus.mockResolvedValue(status)
  grantComputerUsePermissions.mockResolvedValue({ name: 'computer-use-permissions', ok: true, pid: 123 })
  getActionStatus.mockResolvedValue({
    exit_code: 0,
    lines: [],
    name: 'computer-use-permissions',
    pid: null,
    running: false
  })
})

afterEach(() => {
  cleanup()
  resetComputerUseGrantLedger()
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('ComputerUsePanel capability scope', () => {
  it('keeps status, permission grant, and action polling on the selected backend profile', async () => {
    const { ComputerUsePanel } = await import('./computer-use-panel')
    render(<ComputerUsePanel profile={scope} />)

    const grant = await screen.findByRole('button', { name: 'Grant permissions' })
    expect(getComputerUseStatus).toHaveBeenCalledWith(scope)

    vi.useFakeTimers()
    await act(async () => {
      fireEvent.click(grant)
      await Promise.resolve()
    })
    expect(grantComputerUsePermissions).toHaveBeenCalledWith(scope)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500)
    })

    expect(getActionStatus).toHaveBeenCalledWith('computer-use-permissions', 200, scope)
  })

  it('ignores a stale status response after the selected backend profile changes', async () => {
    let resolveFirst!: (value: ComputerUseStatus) => void
    const firstStatus = new Promise<ComputerUseStatus>(resolve => {
      resolveFirst = resolve
    })
    const readyStatus: ComputerUseStatus = {
      ...status,
      accessibility: true,
      ready: true,
      screen_recording: true,
      screen_recording_capturable: true
    }
    getComputerUseStatus.mockImplementation((selected: typeof scope) =>
      selected.profile === scope.profile ? firstStatus : Promise.resolve(readyStatus)
    )

    const { ComputerUsePanel } = await import('./computer-use-panel')
    const view = render(<ComputerUsePanel profile={scope} />)
    view.rerender(<ComputerUsePanel profile={otherScope} />)

    expect(await screen.findByText('Computer Use is ready. Ask the agent to capture an app and click around.')).toBeTruthy()

    await act(async () => {
      resolveFirst(status)
      await Promise.resolve()
    })

    expect(screen.queryByRole('button', { name: 'Grant permissions' })).toBeNull()
    expect(screen.getByText('Computer Use is ready. Ask the agent to capture an app and click around.')).toBeTruthy()
  })

  it('stops polling a permission grant when the selected backend profile changes', async () => {
    let resolveGrant!: (value: { name: string; ok: boolean; pid: number }) => void
    const started = new Promise<{ name: string; ok: boolean; pid: number }>(resolve => {
      resolveGrant = resolve
    })
    const readyStatus: ComputerUseStatus = {
      ...status,
      accessibility: true,
      ready: true,
      screen_recording: true,
      screen_recording_capturable: true
    }
    getComputerUseStatus.mockImplementation((selected: typeof scope) =>
      Promise.resolve(selected.profile === scope.profile ? status : readyStatus)
    )
    grantComputerUsePermissions.mockReturnValue(started)

    const { ComputerUsePanel } = await import('./computer-use-panel')
    const view = render(<ComputerUsePanel profile={scope} />)
    const grant = await screen.findByRole('button', { name: 'Grant permissions' })

    vi.useFakeTimers()
    await act(async () => {
      fireEvent.click(grant)
      await Promise.resolve()
    })
    view.rerender(<ComputerUsePanel profile={otherScope} />)

    await act(async () => {
      resolveGrant({ name: 'computer-use-permissions', ok: true, pid: 123 })
      await vi.runAllTimersAsync()
    })

    expect(getActionStatus).not.toHaveBeenCalled()
    expect(screen.getByText('Computer Use is ready. Ask the agent to capture an app and click around.')).toBeTruthy()
  })

  it('reattaches a live grant after switching away and back instead of spawning again', async () => {
    const readyStatus: ComputerUseStatus = {
      ...status,
      accessibility: true,
      ready: true,
      screen_recording: true,
      screen_recording_capturable: true
    }
    getComputerUseStatus.mockImplementation((selected: typeof scope) =>
      Promise.resolve(selected.profile === scope.profile ? status : readyStatus)
    )
    getActionStatus.mockResolvedValue({
      exit_code: null,
      lines: [],
      name: 'computer-use-grant',
      pid: 123,
      running: true
    })
    grantComputerUsePermissions.mockResolvedValue({ name: 'computer-use-grant', ok: true, pid: 123 })

    const { ComputerUsePanel } = await import('./computer-use-panel')
    const view = render(<ComputerUsePanel profile={scope} />)
    const grant = await screen.findByRole('button', { name: 'Grant permissions' })

    vi.useFakeTimers()
    await act(async () => {
      fireEvent.click(grant)
      await Promise.resolve()
    })
    expect(grantComputerUsePermissions).toHaveBeenCalledTimes(1)

    view.rerender(<ComputerUsePanel profile={otherScope} />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.getByText('Computer Use is ready. Ask the agent to capture an app and click around.')).toBeTruthy()

    view.rerender(<ComputerUsePanel profile={scope} />)
    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.getByRole('button', { name: 'Waiting for approval…' })).toBeTruthy()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500)
    })

    expect(grantComputerUsePermissions).toHaveBeenCalledTimes(1)
    expect(getActionStatus).toHaveBeenCalledWith('computer-use-grant', 200, scope)
  })

  it('surfaces a failed reattachment poll instead of rejecting unhandled', async () => {
    const { rememberComputerUseGrant } = await import('./computer-use-grants')
    const { notifyError } = await import('@/store/notifications')

    rememberComputerUseGrant(scope, 'computer-use-grant')
    getActionStatus.mockRejectedValue(new Error('remote status failed'))

    const rejections: unknown[] = []
    const onUnhandled = (reason: unknown) => {
      rejections.push(reason)
    }

    process.on('unhandledRejection', onUnhandled)
    vi.useFakeTimers()

    try {
      const { ComputerUsePanel } = await import('./computer-use-panel')

      render(<ComputerUsePanel profile={scope} />)
      await act(async () => {
        await Promise.resolve()
      })
      expect(screen.getByRole('button', { name: 'Waiting for approval…' })).toBeTruthy()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_500)
      })

      expect(notifyError).toHaveBeenCalled()
      expect(rejections).toEqual([])
    } finally {
      process.off('unhandledRejection', onUnhandled)
    }
  })

  it('completes an existing-grant retry through the shared post-poll path', async () => {
    // A grant remembered AFTER mount (started by a previous panel mount that
    // unmounted mid-poll) misses the mount reattach, so the Grant click takes
    // the existing-grant retry branch. That branch used to return straight
    // after the poll — no status re-read, no parent notification — leaving the
    // card and the "Configured" pill stale until a manual recheck.
    const { rememberComputerUseGrant } = await import('./computer-use-grants')
    const onConfiguredChange = vi.fn()

    const { ComputerUsePanel } = await import('./computer-use-panel')
    render(<ComputerUsePanel onConfiguredChange={onConfiguredChange} profile={scope} />)

    const grant = await screen.findByRole('button', { name: 'Grant permissions' })
    const statusCallsOnMount = getComputerUseStatus.mock.calls.length

    rememberComputerUseGrant(scope, 'computer-use-grant')

    vi.useFakeTimers()
    await act(async () => {
      fireEvent.click(grant)
      await vi.advanceTimersByTimeAsync(1_500)
    })

    // The retry must NOT spawn a second permission grant...
    expect(grantComputerUsePermissions).not.toHaveBeenCalled()
    // ...it polls the remembered action...
    expect(getActionStatus).toHaveBeenCalledWith('computer-use-grant', 200, scope)
    // ...then completes exactly like a fresh spawn: status re-read and the
    // parent told about the capability change.
    expect(getComputerUseStatus.mock.calls.length).toBe(statusCallsOnMount + 1)
    expect(onConfiguredChange).toHaveBeenCalledOnce()
  })
})
