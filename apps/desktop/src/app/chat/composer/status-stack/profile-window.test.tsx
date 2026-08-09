import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $activeGatewayProfile } from '@/store/profile'
import { $activeSessionId, $selectedStoredSessionId, $sessions } from '@/store/session'
import { $subagentsBySession, type SubagentProgress } from '@/store/subagents'
import type * as WindowsStore from '@/store/windows'
import type { SessionInfo } from '@/types/hermes'

import { ComposerStatusStack } from './index'

const openSessionInNewWindow = vi.fn()

vi.mock('@/store/windows', async importOriginal => ({
  ...(await importOriginal<typeof WindowsStore>()),
  openSessionInNewWindow: (...args: unknown[]) => openSessionInNewWindow(...args)
}))

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', ResizeObserverStub)

const RUNTIME = 'parent-runtime'
const STORED = 'parent-stored'

const subagent = (): SubagentProgress => ({
  filesRead: [],
  filesWritten: [],
  goal: 'Research docs',
  id: 'sub-1',
  parentId: null,
  sessionId: 'child-stored',
  startedAt: 0,
  status: 'running',
  stream: [],
  taskCount: 1,
  taskIndex: 0,
  updatedAt: 0
})

function renderStack() {
  return render(
    <MemoryRouter>
      <I18nProvider configClient={null} initialLocale="en">
        <ComposerStatusStack queue={null} sessionId={RUNTIME} />
      </I18nProvider>
    </MemoryRouter>
  )
}

describe('ComposerStatusStack subagent windows', () => {
  beforeEach(() => {
    openSessionInNewWindow.mockReset()
    $activeGatewayProfile.set('default')
    $activeSessionId.set(RUNTIME)
    $selectedStoredSessionId.set(STORED)
    $sessions.set([{ id: STORED, profile: 'life' } as SessionInfo])
    $subagentsBySession.set({ [RUNTIME]: [subagent()] })
  })

  afterEach(() => {
    cleanup()
    $activeGatewayProfile.set('default')
    $activeSessionId.set(null)
    $selectedStoredSessionId.set(null)
    $sessions.set([])
    $subagentsBySession.set({})
  })

  it('opens a child watch window against the parent session profile', () => {
    renderStack()

    fireEvent.click(screen.getByText('1 Subagent'))
    fireEvent.click(screen.getByText('Research docs'))

    expect(openSessionInNewWindow).toHaveBeenCalledWith('child-stored', { profile: 'life', watch: true })
  })
})
