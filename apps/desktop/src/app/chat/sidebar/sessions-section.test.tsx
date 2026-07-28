import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { sessionIdentityKey } from '@/lib/session-identity'

const sortableMocks = vi.hoisted(() => ({
  listIds: [] as string[][],
  rowIds: [] as string[],
  virtualRowIds: [] as string[]
}))

vi.mock('@dnd-kit/sortable', () => ({
  useSortable: ({ id }: { id: string }) => {
    sortableMocks.virtualRowIds.push(id)

    return {
      attributes: {},
      isDragging: false,
      listeners: {},
      setNodeRef: vi.fn(),
      transform: null,
      transition: undefined
    }
  }
}))

vi.mock('./reorderable-list', () => ({
  ReorderableList: ({ children, ids }: { children: React.ReactNode; ids: string[] }) => {
    sortableMocks.listIds.push(ids)

    return <div data-testid="sortable-session-list">{children}</div>
  },
  useSortableBindings: (id: string) => {
    sortableMocks.rowIds.push(id)

    return {}
  }
}))

import { SidebarSessionsSection } from './sessions-section'

const crossProfileSession: SessionInfo = {
  ended_at: null,
  id: 'telegram-session',
  input_tokens: 0,
  is_active: false,
  last_active: 1,
  message_count: 2,
  model: null,
  output_tokens: 0,
  preview: 'remote thread',
  profile: 'ubuntu-server',
  source: 'telegram',
  started_at: 1,
  title: 'Cross Profile Chat',
  tool_call_count: 0
}

describe('SidebarSessionsSection profile routing', () => {
  beforeEach(() => {
    sortableMocks.listIds.length = 0
    sortableMocks.rowIds.length = 0
    sortableMocks.virtualRowIds.length = 0
  })

  it('passes the session owner when a row is resumed', () => {
    const onResumeSession = vi.fn()

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={null}
        label="Sessions"
        onArchiveSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onResumeSession={onResumeSession}
        onToggle={vi.fn()}
        onTogglePin={vi.fn()}
        open
        pinned={false}
        sessions={[crossProfileSession]}
        workingSessionIdSet={new Set()}
      />
    )

    fireEvent.click(screen.getByText('Cross Profile Chat'))

    expect(onResumeSession).toHaveBeenCalledWith('telegram-session', 'ubuntu-server')
  })

  it('isolates selected and working state for colliding stored ids', () => {
    const alpha = { ...crossProfileSession, id: 'shared', profile: 'alpha', title: 'Alpha Chat' }
    const beta = { ...crossProfileSession, id: 'shared', profile: 'beta', title: 'Beta Chat' }

    render(
      <SidebarSessionsSection
        activeSessionId={sessionIdentityKey('shared', 'beta')}
        emptyState={null}
        label="Sessions"
        onArchiveSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onResumeSession={vi.fn()}
        onToggle={vi.fn()}
        onTogglePin={vi.fn()}
        open
        pinned={false}
        sessions={[alpha, beta]}
        workingSessionIdSet={new Set([sessionIdentityKey('shared', 'alpha')])}
      />
    )

    expect(screen.getByText('Alpha Chat').closest('[data-working]')?.getAttribute('data-working')).toBe('true')
    expect(screen.getByText('Alpha Chat').closest('[aria-current]')).toBeNull()
    expect(screen.getByText('Beta Chat').closest('[data-working]')).toBeNull()
    expect(screen.getByText('Beta Chat').closest('[aria-current]')?.getAttribute('aria-current')).toBe('page')
  })

  it('uses compound identities for drag-and-drop rows with colliding stored ids', () => {
    const alpha = { ...crossProfileSession, id: 'shared', profile: 'alpha', title: 'Alpha Chat' }
    const beta = { ...crossProfileSession, id: 'shared', profile: 'beta', title: 'Beta Chat' }
    const identities = [sessionIdentityKey('shared', 'alpha'), sessionIdentityKey('shared', 'beta')]

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={null}
        label="Sessions"
        onArchiveSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onReorderSessions={vi.fn()}
        onResumeSession={vi.fn()}
        onToggle={vi.fn()}
        onTogglePin={vi.fn()}
        open
        pinned={false}
        sessions={[alpha, beta]}
        sortable
        workingSessionIdSet={new Set()}
      />
    )

    expect(sortableMocks.listIds).toEqual([identities])
    expect(sortableMocks.rowIds).toEqual(identities)
  })

  it('uses compound identities for virtualized drag-and-drop rows', () => {
    const sessions = Array.from({ length: 25 }, (_, index) => ({
      ...crossProfileSession,
      id: index < 2 ? 'shared' : `session-${index}`,
      profile: index === 1 ? 'beta' : 'alpha',
      title: `Session ${index}`
    }))

    const identities = sessions.map(session => sessionIdentityKey(session.id, session.profile))

    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={null}
        label="Sessions"
        onArchiveSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onReorderSessions={vi.fn()}
        onResumeSession={vi.fn()}
        onToggle={vi.fn()}
        onTogglePin={vi.fn()}
        open
        pinned={false}
        sessions={sessions}
        sortable
        workingSessionIdSet={new Set()}
      />
    )

    expect(sortableMocks.listIds).toEqual([identities])
    expect(sortableMocks.virtualRowIds).toContain(sessionIdentityKey('shared', 'alpha'))
    expect(sortableMocks.virtualRowIds).toContain(sessionIdentityKey('shared', 'beta'))
  })
})
