import { describe, expect, it } from 'vitest'

import {
  formatElapsed,
  formatTokenFlow,
  formatTurnSummary,
  TurnSummaryCollector,
} from './turn-summary'

describe('formatElapsed', () => {
  it('formats sub-minute seconds with one decimal', () => {
    expect(formatElapsed(-5)).toBe('0.0s')
    expect(formatElapsed(0)).toBe('0.0s')
    expect(formatElapsed(1.0)).toBe('1.0s')
    expect(formatElapsed(12.4)).toBe('12.4s')
    expect(formatElapsed(59.9)).toBe('59.9s')
  })

  it('formats minutes with zero-padded seconds', () => {
    expect(formatElapsed(60)).toBe('1m00s')
    expect(formatElapsed(125)).toBe('2m05s')
    expect(formatElapsed(3605)).toBe('60m05s')
  })
})

describe('TurnSummaryCollector / formatTurnSummary', () => {
  it('renders single and multiple nouns with correct pluralization', () => {
    const collector = new TurnSummaryCollector()
    collector.recordTool('read_file')
    expect(collector.render(1.0)).toBe('⋯ 1.0s · read 1 file')

    collector.begin()
    collector.recordTool('read_file')
    collector.recordTool('read_file')
    collector.recordTool('read_file')
    expect(collector.render(1.0)).toBe('⋯ 1.0s · read 3 files')
  })

  it('handles irregular pluralization (ies/ses and compound nouns)', () => {
    const c = new TurnSummaryCollector()
    c.recordTool('memory')
    expect(c.render(1.0)).toBe('⋯ 1.0s · updated 1 memory')

    c.begin()
    c.recordTool('memory')
    c.recordTool('memory')
    expect(c.render(1.0)).toBe('⋯ 1.0s · updated 2 memories')

    c.begin()
    c.recordTool('todo')
    expect(c.render(1.0)).toBe('⋯ 1.0s · updated 1 task list')

    c.begin()
    c.recordTool('todo')
    c.recordTool('todo')
    expect(c.render(1.0)).toBe('⋯ 1.0s · updated 2 task lists')

    c.begin()
    c.recordTool('web_search')
    expect(c.render(1.0)).toBe('⋯ 1.0s · searched the web 1 time')
  })

  it('renders edit tools with line deltas from object and JSON patch results', () => {
    const c = new TurnSummaryCollector()
    c.recordTool('patch', { result: { diff: '--- a/f\n+++ b/f\n+line 1\n+line 2\n-line 3' } })
    c.recordTool('write_file')
    expect(c.render(12.4)).toBe('⋯ 12.4s · edited 2 files +2 -1')

    c.begin()
    c.recordTool('patch', { result: JSON.stringify({ diff: '--- a\n+++ b\n+added' }) })
    expect(c.render(2.0)).toBe('⋯ 2.0s · edited 1 file +1 -0')
  })

  it('ignores +0 -0 diffs and does not set hasLineDeltas', () => {
    const c = new TurnSummaryCollector()
    c.recordTool('patch', { result: { diff: '--- a/f\n+++ b/f\n' } })
    expect(c.tally.hasLineDeltas).toBe(false)
    expect(c.render(1.0)).toBe('⋯ 1.0s · edited 1 file')
  })

  it('buckets unknown tools into a called-N-tools segment', () => {
    const c = new TurnSummaryCollector()
    c.recordTool('unknown_tool_a')
    expect(c.render(1.0)).toBe('⋯ 1.0s · called 1 tool')

    c.recordTool('unknown_tool_b')
    expect(c.render(1.0)).toBe('⋯ 1.0s · called 2 tools')
  })

  it('ignores errors and underscore-prefixed internal tools', () => {
    const c = new TurnSummaryCollector()
    c.recordTool('read_file', { isError: true })
    c.recordTool('_thinking')
    c.recordTool(null)
    c.recordTool(undefined)
    c.recordTool('')
    expect(c.tally.totalTools).toBe(0)
    expect(c.render(1.0)).toBe('')
  })

  it('returns empty string for toolless turns under the 2s threshold', () => {
    expect(formatTurnSummary(1.0, null)).toBe('')
    const c = new TurnSummaryCollector()
    expect(c.render(1.5)).toBe('')
  })

  it('renders elapsed-only line for toolless turns at/over the 2s threshold (Python parity)', () => {
    const c = new TurnSummaryCollector()
    expect(c.render(2.0)).toBe('⋯ 2.0s')
    expect(c.render(5.0)).toBe('⋯ 5.0s')
  })

  it('collapses excess verb segments at maxSegments, keeping elapsed prepended', () => {
    const c = new TurnSummaryCollector()
    c.recordTool('patch') // edited
    c.recordTool('read_file') // read
    c.recordTool('terminal') // ran
    c.recordTool('web_search') // searched the web
    c.recordTool('unknown_tool') // called 1 tool
    // 5 verb segments; maxSegments=4 keeps 4 and appends +1 more
    expect(formatTurnSummary(10.0, c.tally, { maxSegments: 4 })).toBe(
      '⋯ 10.0s · edited 1 file · read 1 file · ran 1 command · searched the web 1 time · +1 more',
    )
  })

  it('orders edited, read, ran before other verbs in first-seen order', () => {
    const c = new TurnSummaryCollector()
    c.recordTool('memory') // updated
    c.recordTool('write_file') // edited
    c.recordTool('terminal') // ran
    c.recordTool('read_file') // read
    expect(c.render(3.0)).toBe('⋯ 3.0s · edited 1 file · read 1 file · ran 1 command · updated 1 memory')
  })
})

describe('formatTokenFlow', () => {
  it('returns empty for non-positive or unparseable input', () => {
    expect(formatTokenFlow(null)).toBe('')
    expect(formatTokenFlow(undefined)).toBe('')
    expect(formatTokenFlow(0)).toBe('')
    expect(formatTokenFlow(-100)).toBe('')
    expect(formatTokenFlow('invalid')).toBe('')
  })

  it('formats positive counts with k/M suffixes', () => {
    expect(formatTokenFlow(123)).toBe('↓ 123 tok')
    expect(formatTokenFlow('123')).toBe('↓ 123 tok')
    expect(formatTokenFlow(1000)).toBe('↓ 1k tok')
    expect(formatTokenFlow(1200)).toBe('↓ 1.2k tok')
    expect(formatTokenFlow(1_000_000)).toBe('↓ 1M tok')
    expect(formatTokenFlow(1_200_000)).toBe('↓ 1.2M tok')
  })

  it('honors a custom arrow', () => {
    expect(formatTokenFlow(500, { arrow: '→' })).toBe('→ 500 tok')
  })
})
