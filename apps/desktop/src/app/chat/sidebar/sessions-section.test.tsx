import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { SidebarSessionsSection } from './sessions-section'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: { sidebar: { dateDivider: { lastWeek: 'Last week', older: 'Older', today: 'Today', yesterday: 'Yesterday' } } }
  })
}))

const renderedRowProps = vi.hoisted(() => new Map<string, Record<string, unknown>>())

vi.mock('./session-row', () => ({
  SidebarSessionRow: (props: { onResume: () => void; session: SessionInfo }) => {
    renderedRowProps.set(`${props.session.profile}-${props.session.id}`, props as unknown as Record<string, unknown>)

    return (
      <button
        data-testid={`session-${props.session.profile}-${props.session.id}`}
        onClick={props.onResume}
        type="button"
      >
        {props.session.profile}/{props.session.id}
      </button>
    )
  }
}))

const session = (profile: string, source = 'desktop'): SessionInfo =>
  ({
    archived: false,
    cwd: null,
    ended_at: null,
    id: 'same',
    input_tokens: 0,
    is_active: false,
    last_active: 100,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    profile,
    source,
    started_at: 100,
    title: `${profile} same`,
    tool_call_count: 0
  }) as SessionInfo

describe('Sessions cron rows', () => {
  it('opens a cron row with its actual owner', () => {
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
        sessions={[session('work', 'cron')]}
        workingSessionIdSet={new Set()}
      />
    )

    fireEvent.click(screen.getByTestId('session-work-same'))

    expect(onResumeSession).toHaveBeenCalledWith('same', 'work')
  })

  it('leaves ordinary row resume behavior profile-agnostic', () => {
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
        sessions={[session('work')]}
        workingSessionIdSet={new Set()}
      />
    )

    fireEvent.click(screen.getByTestId('session-work-same'))

    expect(onResumeSession).toHaveBeenCalledWith('same', undefined)
  })

  it('does not provide archive or delete actions for cron rows', () => {
    render(
      <SidebarSessionsSection
        activeSessionId={null}
        emptyState={null}
        label="Sessions"
        onArchiveSession={vi.fn()}
        onDeleteSession={vi.fn()}
        onResumeSession={vi.fn()}
        onToggle={vi.fn()}
        onTogglePin={vi.fn()}
        open
        pinned={false}
        sessions={[session('work', 'cron')]}
        workingSessionIdSet={new Set()}
      />
    )

    const props = renderedRowProps.get('work-same')

    expect(props?.hideDestructiveActions).toBe(true)
    expect(props?.onArchive).toBeTypeOf('function')
    expect(props?.onDelete).toBeTypeOf('function')
    expect(props?.onResume).toBeTypeOf('function')
  })
})
