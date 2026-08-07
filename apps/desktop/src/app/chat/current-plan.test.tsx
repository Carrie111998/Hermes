import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'
import { type ChatMessage, textPart } from '@/lib/chat-messages'
import { createClientSessionState } from '@/lib/chat-runtime'
import type { TodoItem } from '@/lib/todos'
import { $activeSessionId } from '@/store/session'
import { $sessionStates } from '@/store/session-states'
import { $todosBySession, clearSessionTodos, setSessionTodos } from '@/store/todos'

import { CurrentPlanPanel, CurrentPlanSurface, RoutedCurrentPlanSurface } from './current-plan'
import { type SessionView, SessionViewProvider } from './session-view'

const todos: TodoItem[] = [
  { content: 'Inspect persisted history', id: 'inspect', status: 'completed' },
  { content: 'Do not claim liveness', id: 'liveness', status: 'in_progress' },
  { content: 'Keep future work visible', id: 'future', status: 'pending' },
  { content: 'Skip obsolete path', id: 'skip', status: 'cancelled' }
]

const planPart = (items: TodoItem[], updatedAt = 20) => ({
  type: 'tool-call' as const,
  toolCallId: 'todo-1',
  toolName: 'todo',
  args: { todos: items } as never,
  result: { todos: items },
  todoUpdatedAt: updatedAt
})

const transcript = (label: string, items = todos): ChatMessage[] => [
  { id: `${label}-user`, parts: [textPart(`Request ${label}`)], role: 'user', timestamp: 10 },
  { id: `${label}-assistant`, parts: [planPart(items)], role: 'assistant', timestamp: 25 }
]

function viewFor(messages: ChatMessage[], options: { busy?: boolean; runtimeId?: string | null; storedId?: string } = {}): SessionView {
  const runtimeId = options.runtimeId === undefined ? 'runtime-1' : options.runtimeId

  return {
    kind: 'primary',
    $awaitingResponse: atom(false),
    $busy: atom(Boolean(options.busy)),
    $cwd: atom(''),
    $fast: atom(false),
    $lastVisibleIsUser: atom(false),
    $messages: atom(messages),
    $messagesEmpty: atom(messages.length === 0),
    $model: atom(''),
    $provider: atom(''),
    $reasoningEffort: atom(''),
    $runtimeId: atom(runtimeId),
    $storedId: atom(options.storedId ?? 'stored-1')
  }
}

afterEach(() => {
  cleanup()
  $activeSessionId.set(null)
  $sessionStates.set({})
})

beforeEach(() => {
  $activeSessionId.set(null)
  $sessionStates.set({})
  $todosBySession.set({})
})

describe('CurrentPlanPanel', () => {
  it('is collapsed by default and expands into a read-only checklist with provenance and liveness warning', () => {
    render(
      <CurrentPlanPanel
        plan={{
          completedCount: 1,
          hasNewerTurnWithoutTodo: false,
          items: todos,
          sourceMessageId: 'assistant-1',
          status: 'paused',
          totalCount: 4,
          turnNumber: 1,
          updatedAt: 20
        }}
        sessionId="stored-1"
      />
    )

    const toggle = screen.getByRole('button', { name: /current plan/i })

    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    const controls = toggle.getAttribute('aria-controls')
    expect(controls).not.toBeNull()
    expect(toggle.getAttribute('aria-label')).toMatch(/Paused.*1\/4 complete/)
    expect(screen.getByText('Paused')).not.toBeNull()
    expect(screen.getByText('1/4 complete')).not.toBeNull()
    expect(screen.queryByText('Inspect persisted history')).toBeNull()

    fireEvent.click(toggle)

    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    expect(toggle.ownerDocument.getElementById(controls!)).not.toBeNull()
    expect(screen.getByText('Inspect persisted history')).not.toBeNull()
    expect(screen.getByText('Previously in progress')).not.toBeNull()
    expect(screen.getByText(/does not prove that a worker, process, or delegation is running/i)).not.toBeNull()
    expect(screen.getByText(/Turn 1/)).not.toBeNull()
    expect(screen.getByText(/stored-1/)).not.toBeNull()

    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
  })

  it('provides a visible non-ring keyboard focus treatment for the disclosure', () => {
    render(
      <CurrentPlanPanel
        plan={{
          completedCount: 1,
          hasNewerTurnWithoutTodo: false,
          items: todos,
          sourceMessageId: 'assistant-focus',
          status: 'paused',
          totalCount: 4,
          turnNumber: 1,
          updatedAt: 20
        }}
        sessionId="stored-focus"
      />
    )

    const toggle = screen.getByRole('button', { name: /current plan/i })

    expect(toggle.className).toContain('focus-visible:bg-(--ui-control-active-background)')
    expect(toggle.className).toContain('focus-visible:text-foreground')
  })

  it('localizes disclosure accessibility, status, turn, and item-state copy in Arabic', () => {
    render(
      <I18nProvider configClient={null} initialLocale="ar">
        <CurrentPlanPanel
          plan={{
            completedCount: 1,
            hasNewerTurnWithoutTodo: false,
            items: todos,
            sourceMessageId: 'assistant-ar',
            status: 'paused',
            totalCount: 4,
            turnNumber: 1,
            updatedAt: 20
          }}
          sessionId="stored-ar"
        />
      </I18nProvider>
    )

    const toggle = screen.getByRole('button', { name: /توسيع الخطة الحالية، متوقفة مؤقتا، اكتمل 1\/4/ })
    fireEvent.click(toggle)

    expect(screen.getByText(/الدور 1/)).not.toBeNull()
    expect(screen.getByText('كانت قيد التنفيذ')).not.toBeNull()
    expect(toggle.getAttribute('aria-label')).toMatch(/طي الخطة الحالية/)
  })
})

describe('CurrentPlanSurface', () => {
  it('does not duplicate the checklist while a turn is active', () => {
    render(
      <SessionViewProvider value={viewFor(transcript('active'), { busy: true })}>
        <CurrentPlanSurface />
      </SessionViewProvider>
    )

    expect(screen.queryByRole('button', { name: /current plan/i })).toBeNull()
  })

  it.each([
    ['runtime A', createClientSessionState('stored-a')],
    ['an absent runtime binding', null]
  ])('suppresses the previous plan when route B and selection B coexist with %s', (_label, runtimeState) => {
    $activeSessionId.set('runtime-a')
    $sessionStates.set(runtimeState ? { 'runtime-a': runtimeState } : {})

    render(
      <MemoryRouter initialEntries={['/stored-b']}>
        <SessionViewProvider
          value={viewFor(transcript('session-a'), { runtimeId: 'runtime-a', storedId: 'stored-b' })}
        >
          <RoutedCurrentPlanSurface />
        </SessionViewProvider>
      </MemoryRouter>
    )

    expect(screen.queryByRole('button', { name: /current plan/i })).toBeNull()
  })

  it('suppresses a stale selected plan on the New Chat route', () => {
    $activeSessionId.set('runtime-a')
    $sessionStates.set({ 'runtime-a': createClientSessionState('stored-a') })

    render(
      <MemoryRouter initialEntries={['/']}>
        <SessionViewProvider
          value={viewFor(transcript('session-a'), { runtimeId: 'runtime-a', storedId: 'stored-a' })}
        >
          <RoutedCurrentPlanSurface />
        </SessionViewProvider>
      </MemoryRouter>
    )

    expect(screen.queryByRole('button', { name: /current plan/i })).toBeNull()
  })

  it('renders through the route-aware composition once route, selection, and runtime agree', () => {
    $activeSessionId.set('runtime-b')
    $sessionStates.set({ 'runtime-b': createClientSessionState('stored-b') })

    render(
      <MemoryRouter initialEntries={['/stored-b']}>
        <SessionViewProvider
          value={viewFor(transcript('session-b'), { runtimeId: 'runtime-b', storedId: 'stored-b' })}
        >
          <RoutedCurrentPlanSurface />
        </SessionViewProvider>
      </MemoryRouter>
    )

    expect(screen.getByRole('button', { name: /current plan/i })).not.toBeNull()
  })

  it('waits for the finished active-panel linger to clear before showing persisted history', () => {
    const view = viewFor(transcript('finished', [{ content: 'Done', id: 'done', status: 'completed' }]))
    setSessionTodos('runtime-1', [{ content: 'Done', id: 'done', status: 'completed' }])

    const { rerender } = render(
      <SessionViewProvider value={view}>
        <CurrentPlanSurface />
      </SessionViewProvider>
    )

    expect(screen.queryByRole('button', { name: /current plan/i })).toBeNull()

    act(() => clearSessionTodos('runtime-1'))
    rerender(
      <SessionViewProvider value={view}>
        <CurrentPlanSurface />
      </SessionViewProvider>
    )

    expect(screen.getByRole('button', { name: /current plan/i })).not.toBeNull()
    expect(screen.getByText('Completed')).not.toBeNull()
  })

  it('does not reveal older persisted history during an explicit empty live clear', () => {
    const view = viewFor(transcript('older', [{ content: 'Older plan', id: 'older', status: 'completed' }]))
    setSessionTodos('runtime-1', [])

    render(
      <SessionViewProvider value={view}>
        <CurrentPlanSurface />
      </SessionViewProvider>
    )

    expect(screen.queryByRole('button', { name: /current plan/i })).toBeNull()
  })

  it('constrains long expanded plans and scrolls the item list inside the panel', () => {
    const longPlan = Array.from({ length: 40 }, (_, index) => ({
      content: `Plan item ${index + 1}`,
      id: `item-${index + 1}`,
      status: 'pending' as const
    }))

    render(
      <SessionViewProvider value={viewFor(transcript('long-plan', longPlan))}>
        <CurrentPlanSurface />
      </SessionViewProvider>
    )

    fireEvent.click(screen.getByRole('button', { name: /current plan/i }))

    const details = screen.getByText('Plan item 1').closest('[data-slot="current-plan-details"]')
    const items = screen.getByText('Plan item 1').closest('[data-slot="current-plan-items"]')

    expect(details?.className).toContain('max-h-')
    expect(details?.className).toContain('overflow-hidden')
    expect(items?.className).toContain('overflow-y-auto')
    expect(screen.getByRole('button', { name: /collapse current plan/i })).not.toBeNull()
  })

  it('shows nothing for a session without todo history', () => {
    render(
      <SessionViewProvider value={viewFor([{ id: 'plain', parts: [textPart('Hello')], role: 'assistant' }])}>
        <CurrentPlanSurface />
      </SessionViewProvider>
    )

    expect(screen.queryByRole('button', { name: /current plan/i })).toBeNull()
  })

  it('re-derives the plan when switching sessions', () => {
    const { rerender } = render(
      <SessionViewProvider
        value={viewFor(transcript('one', [{ content: 'First session', id: 'one', status: 'completed' }]), {
          storedId: 'stored-one'
        })}
      >
        <CurrentPlanSurface />
      </SessionViewProvider>
    )

    fireEvent.click(screen.getByRole('button', { name: /current plan/i }))
    expect(screen.getByText('First session')).not.toBeNull()

    rerender(
      <SessionViewProvider
        value={viewFor(transcript('two', [{ content: 'Second session', id: 'two', status: 'pending' }]), {
          runtimeId: 'runtime-2',
          storedId: 'stored-two'
        })}
      >
        <CurrentPlanSurface />
      </SessionViewProvider>
    )

    expect(screen.getByRole('button', { name: /current plan/i }).getAttribute('aria-expanded')).toBe('false')
    expect(screen.getByText('Paused')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /current plan/i }))
    expect(screen.getByText('Second session')).not.toBeNull()
    expect(screen.queryByText('First session')).toBeNull()
  })

  it('flags a newer turn without a todo update as superseding the visible plan', () => {
    const messages = [
      ...transcript('old', [{ content: 'Old plan', id: 'old', status: 'completed' }]),
      { id: 'new-user', parts: [textPart('New request')], role: 'user' as const, timestamp: 30 },
      { id: 'new-assistant', parts: [textPart('No plan update')], role: 'assistant' as const, timestamp: 40 }
    ]

    render(
      <SessionViewProvider value={viewFor(messages)}>
        <CurrentPlanSurface />
      </SessionViewProvider>
    )

    expect(screen.getByText('Superseded')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /current plan/i }))
    expect(screen.getByText(/newer turn exists without a todo update/i)).not.toBeNull()
  })
})
