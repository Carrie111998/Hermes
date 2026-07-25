import { describe, expect, it } from 'vitest'

import type { AsyncDelegationRecord } from '../gatewayTypes.js'
import type { AgentRow, AgentRowCells } from '../lib/agentRows.js'
import { buildAgentRows, DONE_LINGER_MS, fitAgentRow, PANEL_MAX_ROWS, shortAgentId } from '../lib/agentRows.js'
import type { SubagentProgress } from '../types.js'

const liveSub = (over: Partial<SubagentProgress> = {}): SubagentProgress => ({
  depth: 1,
  goal: 'map auth handshake edge cases',
  id: 's1',
  index: 0,
  notes: [],
  status: 'running',
  taskCount: 0,
  thinking: [],
  toolCount: 3,
  tools: ['read_file'],
  ...over
})

const asyncRec = (over: Partial<AsyncDelegationRecord> = {}): AsyncDelegationRecord => ({
  delegation_id: 'd1',
  dispatched_at: 1000,
  goal: 'patch token-bucket refill race',
  role: 'fixer',
  status: 'running',
  ...over
})

describe('buildAgentRows', () => {
  it('counts running from both live subagents and async delegations', () => {
    const { done, rows, running } = buildAgentRows(
      [liveSub({ id: 'a', status: 'running' }), liveSub({ id: 'b', status: 'completed' })],
      [asyncRec({ delegation_id: 'd1', status: 'running' }), asyncRec({ delegation_id: 'd2', status: 'completed' })],
      2_000_000
    )

    expect(running).toBe(2) // one live + one async
    expect(done).toBe(2) // one live completed + one async completed
    expect(rows).toHaveLength(4)
  })

  it('orders live rows before async rows', () => {
    const { rows } = buildAgentRows([liveSub({ id: 'a' })], [asyncRec()], 2_000_000)

    expect(rows[0].key).toBe('live:a')
    expect(rows[1].key).toBe('async:d1')
  })

  it('marks a completed async delegation as result-ready with a "result ready" detail', () => {
    const { rows } = buildAgentRows([], [asyncRec({ status: 'completed' })], 2_000_000)

    expect(rows[0].resultReady).toBe(true)
    expect(rows[0].detail).toBe('result ready')
  })

  it('surfaces the live subagent last tool as the row detail', () => {
    const { rows } = buildAgentRows([liveSub({ tools: ['read_file', 'bash'] })], [], 2_000_000)

    expect(rows[0].detail).toBe('bash')
    expect(rows[0].resultReady).toBe(false)
  })

  it('clocks a running live subagent from startedAt', () => {
    const now = 100_000
    const { rows } = buildAgentRows([liveSub({ startedAt: now - 12_000, durationSeconds: undefined })], [], now)

    expect(rows[0].elapsedSeconds).toBeCloseTo(12, 0)
  })

  it('freezes async elapsed at completed_at once done', () => {
    const rec = asyncRec({ completed_at: 1044, dispatched_at: 1000, status: 'completed' })
    // Two different `now`s, both inside the linger window: elapsed must not move.
    const early = buildAgentRows([], [rec], 1_044_000 + 1_000)
    const late = buildAgentRows([], [rec], 1_044_000 + 30_000)

    // 1044 - 1000 = 44s, independent of `now`.
    expect(early.rows[0].elapsedSeconds).toBeCloseTo(44, 0)
    expect(late.rows[0].elapsedSeconds).toBeCloseTo(44, 0)
  })

  it('returns an empty result for no agents', () => {
    const { done, rows, running } = buildAgentRows([], [], 1)

    expect(rows).toEqual([])
    expect(running).toBe(0)
    expect(done).toBe(0)
  })

  it('counts a queued live subagent as running', () => {
    const { running } = buildAgentRows([liveSub({ status: 'queued' })], [], 1)
    expect(running).toBe(1)
  })

  it('treats the "done" async status as result-ready (alias of completed)', () => {
    const { done, rows } = buildAgentRows([], [asyncRec({ status: 'done' })], 2_000_000)
    expect(rows[0].resultReady).toBe(true)
    expect(done).toBe(1)
  })

  it('does not count a failed/interrupted agent as running or done', () => {
    const { done, running } = buildAgentRows(
      [liveSub({ status: 'failed' })],
      [asyncRec({ status: 'error' })],
      2_000_000
    )

    expect(running).toBe(0)
    expect(done).toBe(0)
  })

  it('falls back to "agent" when a live subagent has no goal', () => {
    const { rows } = buildAgentRows([liveSub({ goal: '' })], [], 1)
    expect(rows[0].goal).toBe('agent')
  })

  it('uses the async role as the row name, defaulting to "agent"', () => {
    const { rows } = buildAgentRows(
      [],
      [asyncRec({ delegation_id: 'x', role: undefined }), asyncRec({ delegation_id: 'y', role: 'fixer' })],
      1
    )

    expect(rows[0].name).toBe('agent')
    expect(rows[1].name).toBe('fixer')
  })

  it('yields null elapsed for an async record with no dispatched_at', () => {
    const { rows } = buildAgentRows([], [asyncRec({ dispatched_at: undefined })], 1)
    expect(rows[0].elapsedSeconds).toBeNull()
  })

  it('shows the raw status as detail for a still-running async row', () => {
    const { rows } = buildAgentRows([], [asyncRec({ status: 'running' })], 1)
    expect(rows[0].detail).toBe('running')
    expect(rows[0].resultReady).toBe(false)
  })

  it('leaves live elapsed null when neither duration nor startedAt is known', () => {
    const { rows } = buildAgentRows([liveSub({ durationSeconds: undefined, startedAt: undefined })], [], 1)
    expect(rows[0].elapsedSeconds).toBeNull()
  })
})

describe('buildAgentRows batch dedupe', () => {
  const batch = (over: Partial<AsyncDelegationRecord> = {}): AsyncDelegationRecord =>
    asyncRec({ delegation_id: 'batch', subagent_ids: ['s1', 's2'], ...over })

  it('hides the batch row while its own children are live, counting agents once', () => {
    const { rows, running } = buildAgentRows(
      [liveSub({ id: 's1', status: 'running' }), liveSub({ id: 's2', status: 'running' })],
      [batch()],
      2000
    )

    // Two children in flight is two agents, not three: the batch record and its
    // children describe the same work, and the children are the better row.
    expect(running).toBe(2)
    expect(rows.map(r => r.key)).toEqual(['live:s1', 'live:s2'])
  })

  it('keeps the batch row once its children clear at the turn boundary', () => {
    const { rows, running } = buildAgentRows([], [batch()], 2000)

    expect(running).toBe(1)
    expect(rows.map(r => r.key)).toEqual(['async:batch'])
  })

  it('keeps a finished batch row even while children are still live, for the result cue', () => {
    // The `result ready ⏎` cue is the whole point of the finished row, so a
    // completed batch is never suppressed by a stale live child.
    const { done, rows } = buildAgentRows(
      [liveSub({ id: 's1', status: 'running' })],
      [batch({ completed_at: 1044, status: 'completed' })],
      2000
    )

    expect(done).toBe(1)
    expect(rows.some(r => r.key === 'async:batch' && r.resultReady)).toBe(true)
  })

  it('keeps an unrelated batch whose children are not the live ones', () => {
    const { running, rows } = buildAgentRows(
      [liveSub({ id: 'other', status: 'running' })],
      [batch()],
      2000
    )

    expect(running).toBe(2)
    expect(rows.map(r => r.key)).toEqual(['live:other', 'async:batch'])
  })

  it('keeps a batch that reports no subagent_ids at all', () => {
    // Older records predate the projection; suppressing them on an empty list
    // would silently drop every legacy row.
    const { running } = buildAgentRows([liveSub({ id: 's1' })], [batch({ subagent_ids: undefined })], 2000)

    expect(running).toBe(2)
  })
})

describe('buildAgentRows height bound', () => {
  const manyAsync = (n: number, over: Partial<AsyncDelegationRecord> = {}): AsyncDelegationRecord[] =>
    Array.from({ length: n }, (_, i) => asyncRec({ delegation_id: `bg-${i}`, ...over }))

  it('never paints more than PANEL_MAX_ROWS rows and reports the remainder as hidden', () => {
    const { hidden, rows, running } = buildAgentRows([], manyAsync(12), 2_000_000)

    expect(rows).toHaveLength(PANEL_MAX_ROWS)
    expect(hidden).toBe(12 - PANEL_MAX_ROWS)
    // The counts still describe every agent, not just the painted ones.
    expect(running).toBe(12)
  })

  it('ages a finished delegation out of the panel once the linger window passes', () => {
    const rec = asyncRec({ completed_at: 1044, dispatched_at: 1000, status: 'completed' })
    const fresh = buildAgentRows([], [rec], 1_044_000 + DONE_LINGER_MS - 1)
    const stale = buildAgentRows([], [rec], 1_044_000 + DONE_LINGER_MS + 1)

    expect(fresh.rows).toHaveLength(1)
    expect(stale.rows).toHaveLength(0)
    // Aged-out rows are not "hidden" — they are gone on purpose, not truncated.
    expect(stale.hidden).toBe(0)
    expect(stale.done).toBe(1)
  })

  it('keeps in-flight rows when finished rows compete for the same budget', () => {
    const done = Array.from({ length: 10 }, (_, i) =>
      asyncRec({ delegation_id: `done-${i}`, status: 'completed' })
    )

    const { rows } = buildAgentRows([liveSub({ id: 'live-1' })], [...done, asyncRec({ delegation_id: 'hot' })], 2_000_000)

    expect(rows[0].key).toBe('live:live-1')
    expect(rows[1].key).toBe('async:hot')
    expect(rows).toHaveLength(PANEL_MAX_ROWS)
  })

  it('shows every row when the cap is disabled (the /agents overlay case)', () => {
    const { hidden, rows } = buildAgentRows([], manyAsync(12), 2_000_000, 0)

    expect(rows).toHaveLength(12)
    expect(hidden).toBe(0)
  })
})

describe('shortAgentId', () => {
  it('abbreviates to a 4-char prefix when nothing else collides', () => {
    expect(shortAgentId('b7c2f1a9', ['b7c2f1a9', 'e41d0000'])).toBe('b7c2')
  })

  it('grows the prefix until it is unambiguous', () => {
    expect(shortAgentId('b7c2f1a9', ['b7c2f1a9', 'b7c2f000'])).toBe('b7c2f1')
  })

  it('falls back to the full id when one id prefixes another', () => {
    expect(shortAgentId('b7c2', ['b7c2', 'b7c2f1a9'])).toBe('b7c2')
  })

  it('leaves an id shorter than the floor alone', () => {
    expect(shortAgentId('s1', ['s1', 's2'])).toBe('s1')
  })
})

describe('fitAgentRow', () => {
  const row = (over: Partial<AgentRow> = {}): AgentRow => ({
    detail: 'read_file',
    elapsedSeconds: 42,
    goal: 'map auth handshake edge cases',
    id: 'b7c2',
    key: 'async:b7c2f1a9',
    name: 'fixer',
    resultReady: false,
    status: 'running',
    ...over
  })

  const width = (c: AgentRowCells): number =>
    (c.index + c.id + c.name + c.goal + c.elapsed + c.detail + c.ready).length + 2

  it('fits inside the terminal width at a comfortable size', () => {
    expect(width(fitAgentRow(row(), 1, 72))).toBeLessThanOrEqual(72)
  })

  it('never exceeds the width across a sweep of narrow terminals', () => {
    for (let cols = 20; cols <= 120; cols += 1) {
      expect(width(fitAgentRow(row(), 1, cols))).toBeLessThanOrEqual(cols)
    }
  })

  it('drops detail before elapsed, and elapsed before the role name', () => {
    const wide = fitAgentRow(row(), 1, 72)
    const mid = fitAgentRow(row(), 1, 44)
    const narrow = fitAgentRow(row(), 1, 30)

    expect(wide.detail).toContain('read_file')
    expect(mid.detail).toBe('')
    expect(mid.elapsed).toContain('42s')
    expect(narrow.elapsed).toBe('')
  })

  it('always keeps the index and the @id, because the id is what @<id> takes', () => {
    const cells = fitAgentRow(row(), 7, 24)

    expect(cells.index).toContain('7')
    expect(cells.id).toBe('@b7c2 ')
    expect(cells.goal.length).toBeGreaterThan(0)
  })

  it('omits the @ cell entirely for a row with no id', () => {
    expect(fitAgentRow(row({ id: '' }), 1, 72).id).toBe('')
  })

  it('truncates a long goal instead of letting it wrap', () => {
    const long = 'x'.repeat(400)
    const cells = fitAgentRow(row({ goal: long }), 1, 60)

    expect(cells.goal.length).toBeLessThan(long.length)
    expect(width(cells)).toBeLessThanOrEqual(60)
  })

  it('marks a result-ready row with the ⏎ cue when there is room', () => {
    expect(fitAgentRow(row({ detail: 'result ready', resultReady: true }), 1, 72).ready).toBe(' ⏎')
  })
})
