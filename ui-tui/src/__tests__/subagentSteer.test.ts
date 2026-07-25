import { describe, expect, it } from 'vitest'

import { buildAgentRows } from '../lib/agentRows.js'
import {
  parseSteerCommand,
  resolveAsyncSteerTargetId,
  resolveSteerTargetId,
  steerCompletions,
  steerTokenPrefix
} from '../lib/subagentSteer.js'
import type { SubagentProgress } from '../types.js'

const sub = (over: Partial<SubagentProgress> = {}): SubagentProgress => ({
  depth: 1,
  goal: 'patch token-bucket refill race',
  id: 'b7c2',
  index: 0,
  notes: [],
  status: 'running',
  taskCount: 0,
  thinking: [],
  toolCount: 0,
  tools: [],
  ...over
})

describe('parseSteerCommand', () => {
  it('parses "@id text" into token + body', () => {
    expect(parseSteerCommand('@b7c2 switch approach')).toEqual({ body: 'switch approach', token: 'b7c2' })
  })

  it('keeps multi-line steer bodies', () => {
    const cmd = parseSteerCommand('@b7c2 line one\nline two')
    expect(cmd?.token).toBe('b7c2')
    expect(cmd?.body).toBe('line one\nline two')
  })

  it('returns null for a bare @token with no message', () => {
    expect(parseSteerCommand('@b7c2')).toBeNull()
    expect(parseSteerCommand('@b7c2   ')).toBeNull()
  })

  it('returns null for ordinary text that is not a steer', () => {
    expect(parseSteerCommand('email @john the report')).toBeNull()
    expect(parseSteerCommand('just a normal prompt')).toBeNull()
  })

  it('tolerates leading whitespace before the @', () => {
    expect(parseSteerCommand('  @b7c2 go')).toEqual({ body: 'go', token: 'b7c2' })
  })

  it('trims trailing whitespace from the body', () => {
    expect(parseSteerCommand('@b7c2   go now   ')).toEqual({ body: 'go now', token: 'b7c2' })
  })

  it('returns null for a lone @', () => {
    expect(parseSteerCommand('@')).toBeNull()
    expect(parseSteerCommand('@ hi')).toBeNull()
  })
})

describe('resolveSteerTargetId', () => {
  it('resolves an exact live subagent id', () => {
    expect(resolveSteerTargetId('b7c2', [sub({ id: 'b7c2' })])).toBe('b7c2')
  })

  it('resolves a unique id prefix', () => {
    expect(resolveSteerTargetId('b7', [sub({ id: 'b7c2' }), sub({ id: 'a11a' })])).toBe('b7c2')
  })

  it('refuses an ambiguous prefix (never steer a guess)', () => {
    expect(resolveSteerTargetId('b', [sub({ id: 'b7c2' }), sub({ id: 'b999' })])).toBeNull()
  })

  it('ignores finished subagents — only running/queued are addressable', () => {
    expect(resolveSteerTargetId('b7c2', [sub({ id: 'b7c2', status: 'completed' })])).toBeNull()
  })

  it('returns null when nothing matches (falls back to a normal turn)', () => {
    expect(resolveSteerTargetId('nope', [sub({ id: 'b7c2' })])).toBeNull()
  })
})

describe('resolveAsyncSteerTargetId', () => {
  const delegation = (delegation_id: string, status = 'running') => ({ delegation_id, status })

  it('resolves exact and unique-prefix background delegation ids', () => {
    const rows = [delegation('deleg_b7c2'), delegation('deleg_a11a')]

    expect(resolveAsyncSteerTargetId('deleg_b7c2', rows)).toBe('deleg_b7c2')
    expect(resolveAsyncSteerTargetId('deleg_b7', rows)).toBe('deleg_b7c2')
  })

  it('refuses ambiguous or finished background delegation ids', () => {
    expect(resolveAsyncSteerTargetId('deleg_b', [delegation('deleg_b7c2'), delegation('deleg_b999')])).toBeNull()
    expect(resolveAsyncSteerTargetId('deleg_b7c2', [delegation('deleg_b7c2', 'completed')])).toBeNull()
  })
})

describe('steerTokenPrefix', () => {
  it('matches while the composer holds only "@" + a partial id', () => {
    expect(steerTokenPrefix('@')).toBe('')
    expect(steerTokenPrefix('@b7')).toBe('b7')
  })

  it('stops matching once a space starts the steer body — that text is not an id', () => {
    expect(steerTokenPrefix('@b7c2 ')).toBeNull()
    expect(steerTokenPrefix('@b7c2 check the retry budget')).toBeNull()
  })

  it('ignores inputs that are not an @ token at all', () => {
    expect(steerTokenPrefix('')).toBeNull()
    expect(steerTokenPrefix('/help')).toBeNull()
    expect(steerTokenPrefix('email me@example.com')).toBeNull()
  })
})

describe('steerCompletions', () => {
  const delegation = (delegation_id: string, over: Record<string, unknown> = {}) => ({
    delegation_id,
    goal: 'sweep flaky gateway suite',
    status: 'running',
    ...over
  })

  it('offers live subagents and background delegations', () => {
    const rows = steerCompletions('', [sub({ id: 'b7c2aaa' })], [delegation('deleg_a11a')])

    expect(rows.map(r => r.display)).toEqual(['@b7c2', '@dele'])
    expect(rows.map(r => r.text)).toEqual(['@b7c2 ', '@dele '])
  })

  it('abbreviates ids exactly as the panel prints them, including against finished agents', () => {
    // b7c2aaa is done but still on screen, so the panel must disambiguate the
    // running one to `b7c2b`. Completing to `@b7c2` here would offer an id the
    // user can never see, and would drift the moment the finished row ages out.
    const subs = [sub({ id: 'b7c2aaa', status: 'completed' }), sub({ id: 'b7c2bbb' })]
    const delegations = [delegation('deleg_a11a')]

    const panelIds = buildAgentRows(subs, delegations, Date.now()).rows.map(r => r.id)
    const completed = steerCompletions('', subs, delegations)

    expect(panelIds).toContain('b7c2b')

    for (const row of completed) {
      expect(panelIds).toContain(row.display.slice(1))
    }
  })

  it('completes to an id that the resolver then accepts (no completion can insert a dead token)', () => {
    const subs = [sub({ id: 'b7c2aaa' }), sub({ id: 'b7c2bbb' })]
    const rows = steerCompletions('', subs, [])

    for (const row of rows) {
      expect(resolveSteerTargetId(parseSteerCommand(row.text + 'go')!.token, subs)).not.toBeNull()
    }
  })

  it('filters by the typed prefix', () => {
    const subs = [sub({ id: 'b7c2' }), sub({ id: 'a11a' })]

    expect(steerCompletions('b', subs, []).map(r => r.display)).toEqual(['@b7c2'])
    expect(steerCompletions('zz', subs, [])).toEqual([])
  })

  it('only offers steerable agents — finished ones are not addressable', () => {
    const rows = steerCompletions(
      '',
      [sub({ id: 'b7c2', status: 'completed' }), sub({ id: 'a11a', status: 'queued' })],
      [delegation('deleg_done', { status: 'completed' })]
    )

    expect(rows.map(r => r.display)).toEqual(['@a11a'])
  })

  it('labels each row with its kind and a compact goal so the id is recognisable', () => {
    const [live, background] = steerCompletions('', [sub({ id: 'b7c2' })], [delegation('deleg_a11a')])

    expect(live?.meta).toBe('live subagent · patch token-bucket refill race')
    expect(background?.meta).toBe('background · sweep flaky gateway suite')
  })

  it('truncates a long goal instead of letting the dropdown row run away', () => {
    const [row] = steerCompletions('', [sub({ goal: 'x'.repeat(200) })], [])

    expect(row!.meta.length).toBeLessThanOrEqual('live subagent · '.length + 48)
    expect(row!.meta.endsWith('…')).toBe(true)
  })
})
