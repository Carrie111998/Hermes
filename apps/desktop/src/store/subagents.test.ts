import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $subagentsBySession,
  activeSubagentCount,
  allSubagents,
  buildSubagentTree,
  clearSessionSubagents,
  failedSubagentCount,
  pruneDelegateFallbackSubagents,
  pruneFinishedSessionSubagents,
  reconcileActiveSubagents,
  upsertSubagent
} from './subagents'

const listFor = (sid: string) => $subagentsBySession.get()[sid] ?? []

describe('subagent store', () => {
  beforeEach(() => $subagentsBySession.set({}))

  it('upserts subagent progress and keeps terminal status stable', () => {
    upsertSubagent('s1', { goal: 'scan files', status: 'running', subagent_id: 'a1', task_index: 0 })
    upsertSubagent('s1', { goal: 'scan files', status: 'completed', subagent_id: 'a1', summary: 'done', task_index: 0 })
    upsertSubagent('s1', { goal: 'scan files', status: 'running', subagent_id: 'a1', task_index: 0, text: 'late' })

    const item = listFor('s1')[0]
    expect(item?.status).toBe('completed')
    expect(item?.summary).toBe('done')
  })

  it('builds parent/child trees', () => {
    upsertSubagent('s1', { goal: 'parent', status: 'running', subagent_id: 'p', task_index: 0 })
    upsertSubagent('s1', { goal: 'child', parent_id: 'p', status: 'queued', subagent_id: 'c', task_index: 1 })

    const tree = buildSubagentTree(listFor('s1'))
    expect(tree).toHaveLength(1)
    expect(tree[0]?.children[0]?.goal).toBe('child')
    expect(activeSubagentCount(listFor('s1'))).toBe(2)
  })

  it('keeps root nodes in spawn order, not task index order', () => {
    const nowSpy = vi.spyOn(Date, 'now')
    nowSpy.mockReturnValueOnce(1_000)
    upsertSubagent('s1', { goal: 'first spawn', status: 'running', subagent_id: 'a', task_index: 2 })
    nowSpy.mockReturnValueOnce(2_000)
    upsertSubagent('s1', { goal: 'second spawn', status: 'running', subagent_id: 'b', task_index: 0 })
    nowSpy.mockRestore()

    expect(buildSubagentTree(listFor('s1')).map(n => n.id)).toEqual(['a', 'b'])
  })

  it('captures live thinking/progress/tool stream lines', () => {
    upsertSubagent(
      's1',
      { goal: 'scan files', status: 'queued', subagent_id: 'a1', task_index: 0 },
      true,
      'subagent.spawn_requested'
    )
    upsertSubagent(
      's1',
      {
        status: 'running',
        subagent_id: 'a1',
        task_index: 0,
        tool_name: 'search_files',
        tool_preview: 'pattern=hermes'
      },
      false,
      'subagent.tool'
    )
    upsertSubagent(
      's1',
      { status: 'running', subagent_id: 'a1', task_index: 0, text: 'plan the search order' },
      false,
      'subagent.thinking'
    )
    upsertSubagent(
      's1',
      { status: 'running', subagent_id: 'a1', task_index: 0, text: 'found candidate matches' },
      false,
      'subagent.progress'
    )
    upsertSubagent(
      's1',
      { status: 'completed', subagent_id: 'a1', summary: 'search complete', task_index: 0 },
      false,
      'subagent.complete'
    )

    const item = listFor('s1')[0]
    expect(item?.stream.map(e => e.kind)).toEqual(['tool', 'thinking', 'progress', 'summary'])
    expect(item?.stream.find(e => e.kind === 'tool')?.text).toContain('Search Files')
    expect(item?.stream.find(e => e.kind === 'thinking')?.text).toBe('plan the search order')
    expect(item?.stream.find(e => e.kind === 'summary')?.text).toBe('search complete')
  })

  it('prunes delegate fallback rows once native events arrive', () => {
    upsertSubagent('s1', { goal: 'fallback', status: 'running', subagent_id: 'delegate-tool:abc:0', task_index: 0 })
    upsertSubagent('s1', { goal: 'native', status: 'running', subagent_id: 'sa-0-xyz', task_index: 0 })

    pruneDelegateFallbackSubagents('s1')

    expect(listFor('s1').map(item => item.id)).toEqual(['sa-0-xyz'])
  })

  // Contract: the status-bar "Agents" indicator and the Spawn-tree panel read
  // the same scope — every session's subagents — so a count can never point at
  // an empty tree (the desync behind "Agents (N)" vs "No live subagents").
  it('counts running/failed across every session, matching the aggregated tree', () => {
    upsertSubagent('s1', { goal: 'a', status: 'running', subagent_id: 'a', task_index: 0 })
    upsertSubagent('s1', { goal: 'b', status: 'failed', subagent_id: 'b', task_index: 1 })
    upsertSubagent('s2', { goal: 'c', status: 'running', subagent_id: 'c', task_index: 0 })
    upsertSubagent('s2', { goal: 'd', status: 'failed', subagent_id: 'd', task_index: 1 })

    const flat = allSubagents($subagentsBySession.get())
    const indicatorRunning = Object.values($subagentsBySession.get()).reduce((n, l) => n + activeSubagentCount(l), 0)
    const indicatorFailed = Object.values($subagentsBySession.get()).reduce((n, l) => n + failedSubagentCount(l), 0)
    const tree = buildSubagentTree(flat)

    // The active-session-only filter would have reported 1/1 here, not 2/2.
    expect(indicatorRunning).toBe(2)
    expect(indicatorFailed).toBe(2)
    expect(tree).toHaveLength(4)
    expect(indicatorRunning + indicatorFailed).toBe(tree.length)
  })

  it('clears one session without touching another', () => {
    upsertSubagent('s1', { goal: 'one', status: 'running', subagent_id: 'a1', task_index: 0 })
    upsertSubagent('s2', { goal: 'two', status: 'running', subagent_id: 'a2', task_index: 0 })

    clearSessionSubagents('s1')

    expect($subagentsBySession.get().s1).toBeUndefined()
    expect($subagentsBySession.get().s2).toHaveLength(1)
  })

  // Regression test for #64015: still-RUNNING background subagents must survive
  // the per-turn wipe that previously dropped them at message.start. The fix
  // replaces clearSessionSubagents() with pruneFinishedSessionSubagents() at
  // the use-message-stream message.start handler, so only terminal-status rows
  // get filtered out.
  it('pruneFinishedSessionSubagents keeps running/queued and drops terminal rows', () => {
    upsertSubagent('s1', { goal: 'live-a', status: 'running', subagent_id: 'live-a', task_index: 0 })
    upsertSubagent('s1', { goal: 'live-b', status: 'queued', subagent_id: 'live-b', task_index: 1 })
    upsertSubagent('s1', { goal: 'done', status: 'completed', subagent_id: 'done', task_index: 2 })
    upsertSubagent('s1', { goal: 'broken', status: 'failed', subagent_id: 'broken', task_index: 3 })
    upsertSubagent('s1', { goal: 'cancelled', status: 'interrupted', subagent_id: 'cancelled', task_index: 4 })

    pruneFinishedSessionSubagents('s1')

    const ids = listFor('s1')
      .map(item => item.id)
      .sort()

    expect(ids).toEqual(['live-a', 'live-b'])
    expect(activeSubagentCount(listFor('s1'))).toBe(2)
  })

  // Companion test: after prune, a late `subagent.complete` event for a
  // surviving live row must still be accepted by upsertSubagent (the wipe
  // path previously silently dropped these).
  it('surviving live subagents still accept createIfMissing=false completion', () => {
    upsertSubagent('s1', { goal: 'live', status: 'running', subagent_id: 'live', task_index: 0 })

    pruneFinishedSessionSubagents('s1')

    upsertSubagent(
      's1',
      { status: 'completed', subagent_id: 'live', task_index: 0, summary: 'finished later' },
      false,
      'subagent.complete'
    )

    const item = listFor('s1')[0]
    expect(item?.status).toBe('completed')
    expect(item?.summary).toBe('finished later')
  })

  it('pruneFinishedSessionSubagents leaves other sessions untouched', () => {
    upsertSubagent('s1', { goal: 'live', status: 'running', subagent_id: 'a', task_index: 0 })
    upsertSubagent('s1', { goal: 'done', status: 'completed', subagent_id: 'b', task_index: 1 })
    upsertSubagent('s2', { goal: 'live', status: 'running', subagent_id: 'c', task_index: 0 })
    upsertSubagent('s2', { goal: 'done', status: 'completed', subagent_id: 'd', task_index: 1 })

    pruneFinishedSessionSubagents('s1')

    expect(listFor('s1').map(item => item.id)).toEqual(['a'])
    expect(
      listFor('s2')
        .map(item => item.id)
        .sort()
    ).toEqual(['c', 'd'])
  })

  it('rehydrates active subagents from an authoritative gateway snapshot', () => {
    upsertSubagent('runtime-a', {
      goal: 'stale after reconnect',
      status: 'running',
      subagent_id: 'stale',
      task_index: 0
    })

    reconcileActiveSubagents(
      {
        active: [
          {
            goal: 'live after reconnect',
            model: 'test/model',
            origin_ui_session_id: 'runtime-b',
            parent_id: null,
            started_at: 1_800_000_000,
            status: 'running',
            subagent_id: 'live',
            tool_count: 3
          }
        ]
      },
      'default'
    )

    expect(listFor('runtime-a').map(item => item.id)).toEqual(['stale'])
    expect(listFor('runtime-b')).toEqual([
      expect.objectContaining({
        goal: 'live after reconnect',
        id: 'live',
        model: 'test/model',
        startedAt: 1_800_000_000_000,
        status: 'running',
        toolCount: 3
      })
    ])
  })

  it('reaps snapshot-owned rows when the backend no longer reports them active', () => {
    reconcileActiveSubagents(
      {
        active: [
          {
            goal: 'live',
            origin_ui_session_id: 'runtime-a',
            status: 'running',
            subagent_id: 'live'
          }
        ]
      },
      'default'
    )
    upsertSubagent('runtime-a', { goal: 'done', status: 'completed', subagent_id: 'done', task_index: 1 })

    reconcileActiveSubagents({ active: [] }, 'default')

    expect(listFor('runtime-a').map(item => item.id)).toEqual(['done'])
  })

  it('ignores unscoped snapshots from older backends', () => {
    reconcileActiveSubagents(
      { active: [{ goal: 'ambiguous', status: 'running', subagent_id: 'old-backend' }] },
      'default'
    )

    expect($subagentsBySession.get()).toEqual({})
  })

  it('ignores a snapshot row attributed to another profile', () => {
    reconcileActiveSubagents(
      {
        active: [
          {
            goal: 'wrong profile',
            origin_ui_session_id: 'runtime-a',
            profile: 'profile-b',
            status: 'running',
            subagent_id: 'wrong-profile'
          }
        ]
      },
      'profile-a'
    )

    expect($subagentsBySession.get()).toEqual({})
  })

  it('does not clobber richer event metadata with an unchanged active snapshot', () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(2_000_000)

    upsertSubagent('runtime-a', {
      goal: 'live',
      model: 'test/model',
      status: 'running',
      subagent_id: 'live',
      task_index: 0,
      tool_count: 9
    })

    const beforeMap = $subagentsBySession.get()
    const beforeItem = listFor('runtime-a')[0]

    reconcileActiveSubagents(
      {
        active: [
          {
            goal: 'live',
            model: 'test/model',
            origin_ui_session_id: 'runtime-a',
            started_at: 1_000,
            status: 'running',
            subagent_id: 'live',
            tool_count: 0
          }
        ]
      },
      'no-clobber-test'
    )

    expect($subagentsBySession.get()).toBe(beforeMap)
    expect(listFor('runtime-a')[0]).toBe(beforeItem)
    expect(beforeItem?.startedAt).toBe(2_000_000)
    expect(beforeItem?.toolCount).toBe(9)

    nowSpy.mockRestore()
  })

  it('reaps only rows owned by the profile whose snapshot changed', () => {
    reconcileActiveSubagents(
      {
        active: [
          {
            goal: 'profile a',
            origin_ui_session_id: 'runtime-a',
            status: 'running',
            subagent_id: 'profile-a-agent'
          }
        ]
      },
      'profile-a-test'
    )
    reconcileActiveSubagents(
      {
        active: [
          {
            goal: 'profile b',
            origin_ui_session_id: 'runtime-b',
            status: 'running',
            subagent_id: 'profile-b-agent'
          }
        ]
      },
      'profile-b-test'
    )

    reconcileActiveSubagents({ active: [] }, 'profile-a-test')

    expect(listFor('runtime-a')).toEqual([])
    expect(listFor('runtime-b').map(item => item.id)).toEqual(['profile-b-agent'])
  })

  it('does not reap prior rows from an incomplete backend snapshot', () => {
    reconcileActiveSubagents(
      {
        active: [
          {
            goal: 'survives host timeout',
            origin_ui_session_id: 'runtime-incomplete',
            profile: 'incomplete-profile',
            status: 'running',
            subagent_id: 'sa-incomplete'
          }
        ],
        complete: true
      },
      'incomplete-profile'
    )

    reconcileActiveSubagents(
      { active: [], complete: false, errors: ['compute host did not answer'] },
      'incomplete-profile'
    )
    expect(listFor('runtime-incomplete').map(item => item.id)).toEqual(['sa-incomplete'])

    reconcileActiveSubagents({ active: [], complete: true }, 'incomplete-profile')
    expect(listFor('runtime-incomplete')).toEqual([])
  })

  it('promotes a queued event row without replacing its richer metadata', () => {
    const nowSpy = vi.spyOn(Date, 'now').mockReturnValue(3_000_000)

    upsertSubagent('runtime-a', {
      goal: 'queued',
      status: 'queued',
      subagent_id: 'queued-agent',
      task_index: 2,
      tool_count: 4
    })

    reconcileActiveSubagents(
      {
        active: [
          {
            goal: 'queued',
            origin_ui_session_id: 'runtime-a',
            started_at: 1_000,
            status: 'running',
            subagent_id: 'queued-agent',
            tool_count: 0
          }
        ]
      },
      'queued-promotion-test'
    )

    expect(listFor('runtime-a')[0]).toEqual(
      expect.objectContaining({
        id: 'queued-agent',
        startedAt: 3_000_000,
        status: 'running',
        taskIndex: 2,
        toolCount: 4
      })
    )

    nowSpy.mockRestore()
  })
})
