// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type * as Nanostores from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import type { RemoteAttachState } from '@/store/remote-session'

const remote = vi.hoisted(() => {
  const { atom } = require('nanostores') as typeof Nanostores

  return {
    state: atom<RemoteAttachState>({
      host: '',
      port: 8642,
      token: '',
      expiresAt: '',
      sessions: [],
      status: 'idle'
    }),
    attachToSession: vi.fn(async () => {}),
    detachSession: vi.fn(),
    disconnect: vi.fn(),
    pairWithCode: vi.fn(async () => {}),
    refreshSessions: vi.fn(async () => {}),
    sendRemoteChat: vi.fn(async () => {})
  }
})

vi.mock('@/store/remote-session', () => ({
  $remoteAttach: remote.state,
  attachToSession: remote.attachToSession,
  detachSession: remote.detachSession,
  disconnect: remote.disconnect,
  pairWithCode: remote.pairWithCode,
  refreshSessions: remote.refreshSessions,
  sendRemoteChat: remote.sendRemoteChat
}))

import { RemoteView } from './index'

const connectedState: RemoteAttachState = {
  host: 'remote.test',
  port: 9443,
  token: 'attach-token',
  expiresAt: '2026-08-17T12:00:00Z',
  sessions: [
    {
      id: 'session-alpha-1234567890',
      title: 'Alpha',
      status: 'active',
      updated_at: '2026-08-16T12:00:00Z',
      events: []
    },
    {
      id: 'session-beta-1234567890',
      title: null,
      status: 'idle',
      updated_at: '2026-08-16T11:00:00Z',
      events: []
    }
  ],
  status: 'connected'
}

function renderRemote() {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <RemoteView onClose={vi.fn()} />
    </I18nProvider>
  )
}

describe('RemoteView', () => {
  beforeEach(() => {
    remote.state.set({
      host: '',
      port: 8642,
      token: '',
      expiresAt: '',
      sessions: [],
      status: 'idle'
    })
    remote.attachToSession.mockClear()
    remote.detachSession.mockClear()
    remote.disconnect.mockClear()
    remote.pairWithCode.mockClear()
    remote.refreshSessions.mockClear()
    remote.sendRemoteChat.mockClear()
  })

  afterEach(cleanup)

  it('submits the host, port, and normalized pairing code', async () => {
    renderRemote()

    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'lab-host' } })
    fireEvent.change(screen.getByLabelText('Port'), { target: { value: '9000' } })
    fireEvent.change(screen.getByLabelText('Pairing code'), { target: { value: 'ab12cd' } })

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
    })

    expect(remote.pairWithCode).toHaveBeenCalledWith('lab-host', 9000, 'AB12CD')
  })

  it('renders connected sessions, refreshes on mount, and attaches or detaches rows', async () => {
    remote.state.set({ ...connectedState, attachedSessionId: connectedState.sessions[1].id })

    renderRemote()

    await waitFor(() => expect(remote.refreshSessions).toHaveBeenCalledOnce())
    expect(screen.getByText('Alpha')).toBeTruthy()
    expect(screen.getAllByText('(untitled)').length).toBeGreaterThan(0)
    expect(screen.getByText('active')).toBeTruthy()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Attach to Alpha' }))
    })
    expect(remote.attachToSession).toHaveBeenCalledWith(connectedState.sessions[0].id)

    fireEvent.click(screen.getByRole('button', { name: 'Detach from (untitled)' }))
    expect(remote.detachSession).toHaveBeenCalledOnce()
  })

  it('renders attached events and sends composer text', async () => {
    remote.state.set({
      ...connectedState,
      attachedSessionId: connectedState.sessions[0].id,
      sessions: [
        {
          ...connectedState.sessions[0],
          events: [
            {
              event: 'session.message',
              timestamp: '2026-08-16T12:01:00Z',
              message: { role: 'assistant', content: 'Live answer' }
            },
            { event: 'session.status', timestamp: '2026-08-16T12:02:00Z', status: 'active' },
            {
              event: 'session.tool_call',
              timestamp: '2026-08-16T12:03:00Z',
              tool_call: { name: 'terminal', phase: 'started' }
            }
          ]
        }
      ]
    })

    renderRemote()

    expect(screen.getByText(/Live answer/)).toBeTruthy()
    expect(screen.getByText(/Status changed to active/)).toBeTruthy()
    expect(screen.getByText(/terminal · started/)).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Message remote session'), { target: { value: 'Keep going' } })
    await act(async () => {
      fireEvent.keyDown(screen.getByLabelText('Message remote session'), { key: 'Enter' })
    })

    expect(remote.sendRemoteChat).toHaveBeenCalledWith('Keep going')
    expect((screen.getByLabelText('Message remote session') as HTMLInputElement).value).toBe('')
  })

  it('shows reconnecting state while keeping detach and disconnect available', () => {
    remote.state.set({
      ...connectedState,
      attachedSessionId: connectedState.sessions[0].id,
      status: 'connecting',
      reconnecting: true
    })

    renderRemote()

    expect(screen.getByText('Reconnecting…')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Detach from Alpha' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: 'Disconnect' }).hasAttribute('disabled')).toBe(false)
    expect(screen.getByRole('button', { name: 'Refresh' }).hasAttribute('disabled')).toBe(true)
  })

  it('shows an error banner and restores the pairing form when the token expired', () => {
    remote.state.set({
      ...connectedState,
      token: '',
      expiresAt: '',
      status: 'error',
      error: 'Attach token expired — pair again'
    })

    renderRemote()

    expect(screen.getByRole('alert').textContent).toContain('Attach token expired — pair again')
    expect(screen.getByRole('button', { name: 'Connect' })).toBeTruthy()
  })
})
