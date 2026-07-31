import { describe, expect, it } from 'vitest'

import { latestSessionPlan, latestSessionTodos, parseTodos } from './todos'

describe('parseTodos', () => {
  it('parses todo arrays with valid ids, content, and statuses', () => {
    expect(
      parseTodos([
        { content: 'Gather ingredients', id: 'prep', status: 'completed' },
        { content: 'Boil water', id: 'boil', status: 'in_progress' },
        { content: 'Serve', id: 'serve', status: 'pending' }
      ])
    ).toEqual([
      { content: 'Gather ingredients', id: 'prep', status: 'completed' },
      { content: 'Boil water', id: 'boil', status: 'in_progress' },
      { content: 'Serve', id: 'serve', status: 'pending' }
    ])
  })

  it('parses nested todo payloads from wrapped objects and JSON strings', () => {
    expect(parseTodos({ todos: [{ content: 'Plate', id: 'plate', status: 'pending' }] })).toEqual([
      { content: 'Plate', id: 'plate', status: 'pending' }
    ])

    expect(parseTodos('{"todos":[{"id":"plate","content":"Plate","status":"pending"}]}')).toEqual([
      { content: 'Plate', id: 'plate', status: 'pending' }
    ])
  })

  it('returns null for non-todo payloads', () => {
    expect(parseTodos(undefined)).toBeNull()
    expect(parseTodos('not json')).toBeNull()
    expect(parseTodos({ message: 'no todos here' })).toBeNull()
  })
})

describe('latestSessionTodos', () => {
  const todoPart = (todos: unknown, extra: Record<string, unknown> = {}) => ({
    type: 'tool-call',
    toolCallId: 't1',
    toolName: 'todo',
    args: { todos },
    ...extra
  })

  it('returns the last todo list across the transcript (result beats args)', () => {
    const messages = [
      { parts: [todoPart([{ content: 'Old', id: 'a', status: 'pending' }])] },
      { parts: [{ type: 'text', text: 'hi' }] },
      {
        parts: [
          todoPart([{ content: 'Stale', id: 'a', status: 'pending' }], {
            result: { todos: [{ content: 'Fresh', id: 'a', status: 'completed' }] }
          })
        ]
      }
    ]

    expect(latestSessionTodos(messages)).toEqual([{ content: 'Fresh', id: 'a', status: 'completed' }])
  })

  it('prefers the live carried `todos` field over args', () => {
    const messages = [
      {
        parts: [
          todoPart([{ content: 'Args', id: 'a', status: 'pending' }], {
            todos: [{ content: 'Live', id: 'a', status: 'in_progress' }]
          })
        ]
      }
    ]

    expect(latestSessionTodos(messages)).toEqual([{ content: 'Live', id: 'a', status: 'in_progress' }])
  })

  it('returns null when no todo tool calls exist', () => {
    expect(latestSessionTodos([{ parts: [{ type: 'text', text: 'hi' }] }])).toBeNull()
    expect(latestSessionTodos([])).toBeNull()
  })

  describe('latestSessionPlan', () => {
    const user = (id: string, timestamp: number) => ({ id, parts: [{ type: 'text', text: id }], role: 'user', timestamp })

    const plan = (id: string, todos: unknown, timestamp: number, todoUpdatedAt = timestamp) => ({
      id,
      role: 'assistant',
      timestamp,
      parts: [todoPart(todos, { result: { todos }, todoUpdatedAt })]
    })

    it('identifies the producing turn, exact todo update time, and completion count', () => {
      const snapshot = latestSessionPlan(
        [
          user('u1', 10),
          plan(
            'a1',
            [
              { content: 'Done', id: 'done', status: 'completed' },
              { content: 'Skipped', id: 'skip', status: 'cancelled' }
            ],
            30,
            22
          )
        ],
        { busy: false, hasRuntime: true }
      )

      expect(snapshot).toMatchObject({
        completedCount: 1,
        hasNewerTurnWithoutTodo: false,
        sourceMessageId: 'a1',
        status: 'paused',
        totalCount: 2,
        turnNumber: 1,
        updatedAt: 22
      })
    })

    it('labels unfinished settled work paused without treating in_progress as liveness', () => {
      const snapshot = latestSessionPlan(
        [user('u1', 10), plan('a1', [{ content: 'Was running', id: 'run', status: 'in_progress' }], 20)],
        { busy: false, hasRuntime: true }
      )

      expect(snapshot?.status).toBe('paused')
    })

    it('labels a plan active only from explicit live-turn state', () => {
      const messages = [user('u1', 10), plan('a1', [{ content: 'Running', id: 'run', status: 'in_progress' }], 20)]

      expect(latestSessionPlan(messages, { busy: true, hasRuntime: true })?.status).toBe('active')
      expect(latestSessionPlan(messages, { busy: false, hasRuntime: true })?.status).toBe('paused')
    })

    it('labels the latest plan superseded when a newer user turn has no todo update', () => {
      const snapshot = latestSessionPlan(
        [
          user('u1', 10),
          plan('a1', [{ content: 'Old plan', id: 'old', status: 'completed' }], 20),
          user('u2', 30),
          { id: 'a2', parts: [{ type: 'text', text: 'No new plan' }], role: 'assistant', timestamp: 40 }
        ],
        { busy: false, hasRuntime: true }
      )

      expect(snapshot).toMatchObject({ hasNewerTurnWithoutTodo: true, status: 'superseded', turnNumber: 1 })
    })

    it('uses only the newest todo result and marks an unbound stored session historical', () => {
      const snapshot = latestSessionPlan(
        [
          user('u1', 10),
          plan('a1', [{ content: 'Old', id: 'old', status: 'completed' }], 20),
          user('u2', 30),
          plan('a2', [{ content: 'New', id: 'new', status: 'pending' }], 40)
        ],
        { busy: false, hasRuntime: false }
      )

      expect(snapshot).toMatchObject({ sourceMessageId: 'a2', status: 'historical', turnNumber: 2 })
      expect(snapshot?.items).toEqual([{ content: 'New', id: 'new', status: 'pending' }])
    })

    it('ignores hidden branch results and reports the newer visible turn', () => {
      const snapshot = latestSessionPlan(
        [
          user('u1', 10),
          plan('a1', [{ content: 'Kept', id: 'kept', status: 'completed' }], 20),
          user('u2', 30),
          { ...plan('hidden-a2', [{ content: 'Discarded', id: 'discarded', status: 'completed' }], 40), hidden: true },
          { id: 'retry-a2', parts: [{ type: 'text', text: 'Retried without a plan' }], role: 'assistant', timestamp: 50 }
        ],
        { busy: false, hasRuntime: true }
      )

      expect(snapshot).toMatchObject({
        hasNewerTurnWithoutTodo: true,
        sourceMessageId: 'a1',
        status: 'superseded',
        turnNumber: 1
      })
      expect(snapshot?.items).toEqual([{ content: 'Kept', id: 'kept', status: 'completed' }])
    })

    it('ignores attempted args and result payloads without durable update provenance', () => {
      const attempted = {
        id: 'attempted',
        parts: [todoPart([{ content: 'Attempted', id: 'attempted', status: 'pending' }])],
        role: 'assistant',
        timestamp: 20
      }

      const resultWithoutTimestamp = {
        id: 'unproven',
        parts: [todoPart([], { result: { todos: [{ content: 'Unproven', id: 'unproven', status: 'completed' }] } })],
        role: 'assistant',
        timestamp: 30
      }

      expect(latestSessionPlan([user('u1', 10), attempted, resultWithoutTimestamp], { busy: false, hasRuntime: true })).toBeNull()
    })

    it('labels only fully completed non-empty lists completed', () => {
      const completed = latestSessionPlan(
        [user('u1', 10), plan('done', [{ content: 'Done', id: 'done', status: 'completed' }], 20)],
        { busy: false, hasRuntime: true }
      )

      const cancelled = latestSessionPlan(
        [user('u1', 10), plan('cancelled', [{ content: 'Cancelled', id: 'cancelled', status: 'cancelled' }], 20)],
        { busy: false, hasRuntime: true }
      )

      const empty = latestSessionPlan([user('u1', 10), plan('empty', [], 20)], { busy: false, hasRuntime: true })

      expect(completed?.status).toBe('completed')
      expect(cancelled).toMatchObject({ completedCount: 0, status: 'paused', totalCount: 1 })
      expect(empty).toBeNull()
    })

    it('returns null for sessions with no todo history', () => {
      expect(latestSessionPlan([user('u1', 10)], { busy: false, hasRuntime: true })).toBeNull()
    })
  })
})
