import { describe, expect, it } from 'vitest'

import { sessionMatchesStoredId } from '@/store/session'

import {
  approvalReplaySessionId,
  gatewayEventIsActive,
  gatewayEventRequiresSessionId,
  resolveGatewayEventSessionId
} from './gateway-events'

describe('gateway event routing', () => {
  const lineageSessions = [{ id: 'stored-tip', _lineage_root_id: 'stored-root' }]
  const storedIdsShareLineage = (left: string, right: string) =>
    lineageSessions.some(session => sessionMatchesStoredId(session, left) && sessionMatchesStoredId(session, right))

  it('rehydrates pending approvals on reconnect ready and resumed session info', () => {
    expect(approvalReplaySessionId('gateway.ready', 'active-1', null)).toBe('active-1')
    expect(approvalReplaySessionId('session.info', 'active-1', 'routed-1')).toBe('routed-1')
    expect(approvalReplaySessionId('message.delta', 'active-1', 'routed-1')).toBeNull()
  })

  it('drops only unscoped subagent events (genuinely background work)', () => {
    expect(gatewayEventRequiresSessionId('subagent.progress')).toBe(true)
    expect(gatewayEventRequiresSessionId('subagent.start')).toBe(true)
  })

  it('attributes unscoped foreground turn events to the active chat', () => {
    // These must NOT be dropped when unscoped — they are the focused turn's own
    // output, and dropping them loses the live response until a refetch (#42178).
    expect(gatewayEventRequiresSessionId('message.delta')).toBe(false)
    expect(gatewayEventRequiresSessionId('message.complete')).toBe(false)
    expect(gatewayEventRequiresSessionId('message.interim')).toBe(false)
    expect(gatewayEventRequiresSessionId('reasoning.delta')).toBe(false)
    expect(gatewayEventRequiresSessionId('tool.start')).toBe(false)
    expect(gatewayEventRequiresSessionId('approval.request')).toBe(false)
  })

  it('allows global events to remain unscoped', () => {
    expect(gatewayEventRequiresSessionId('gateway.ready')).toBe(false)
    expect(gatewayEventRequiresSessionId('preview.restart.progress')).toBe(false)
    expect(gatewayEventRequiresSessionId('session.info')).toBe(false)
    expect(gatewayEventRequiresSessionId(undefined)).toBe(false)
  })

  it('keeps unscoped stream events pinned to the session that started them', () => {
    const started = resolveGatewayEventSessionId({
      activeSessionId: 'session-a',
      eventType: 'message.start',
      explicitSessionId: '',
      unscopedStreamSessionId: null
    })

    expect(started).toEqual({
      drop: false,
      nextUnscopedStreamSessionId: 'session-a',
      pinned: false,
      sessionId: 'session-a'
    })

    const delta = resolveGatewayEventSessionId({
      activeSessionId: 'session-b',
      eventType: 'message.delta',
      explicitSessionId: '',
      unscopedStreamSessionId: started.nextUnscopedStreamSessionId
    })

    expect(delta).toEqual({
      drop: false,
      nextUnscopedStreamSessionId: 'session-a',
      pinned: true,
      sessionId: 'session-a'
    })

    const completed = resolveGatewayEventSessionId({
      activeSessionId: 'session-b',
      eventType: 'message.complete',
      explicitSessionId: '',
      unscopedStreamSessionId: delta.nextUnscopedStreamSessionId
    })

    expect(completed).toEqual({
      drop: false,
      nextUnscopedStreamSessionId: null,
      pinned: true,
      sessionId: 'session-a'
    })
  })

  it('routes a new unscoped stream start to the currently active session', () => {
    const routed = resolveGatewayEventSessionId({
      activeSessionId: 'session-b',
      eventType: 'message.start',
      explicitSessionId: '',
      unscopedStreamSessionId: 'session-a'
    })

    expect(routed).toEqual({
      drop: false,
      nextUnscopedStreamSessionId: 'session-b',
      pinned: false,
      sessionId: 'session-b'
    })
  })

  it('attributes an unpinned stream event to the active session without the pin flag', () => {
    // A late straggler (no pin left after the previous turn completed) falls
    // back to the active session. The handler drops this case when the target
    // session has no live turn — the straggler belongs to a turn that already
    // ended elsewhere (#43142 family).
    const routed = resolveGatewayEventSessionId({
      activeSessionId: 'session-b',
      eventType: 'thinking.delta',
      explicitSessionId: '',
      unscopedStreamSessionId: null
    })

    expect(routed).toEqual({
      drop: false,
      nextUnscopedStreamSessionId: null,
      pinned: false,
      sessionId: 'session-b'
    })
  })

  it('keeps explicit events scoped and clears a matching pinned stream on completion', () => {
    const routed = resolveGatewayEventSessionId({
      activeSessionId: 'session-b',
      eventType: 'message.complete',
      explicitSessionId: 'session-a',
      unscopedStreamSessionId: 'session-a'
    })

    expect(routed).toEqual({
      drop: false,
      nextUnscopedStreamSessionId: null,
      pinned: true,
      sessionId: 'session-a'
    })
  })

  it('accepts a recovered runtime for the selected conversation from the active source', () => {
    expect(
      gatewayEventIsActive({
        activeRuntimeSessionId: 'runtime-stale',
        eventRuntimeSessionId: 'runtime-current',
        eventStoredSessionId: 'stored-visible',
        fromActiveSource: true,
        selectedStoredSessionId: 'stored-visible',
        storedIdsShareLineage: (left, right) => left === right
      })
    ).toBe(true)
  })

  it('rejects a stored-session-id collision from an inactive source', () => {
    expect(
      gatewayEventIsActive({
        activeRuntimeSessionId: 'runtime-visible',
        eventRuntimeSessionId: 'runtime-background',
        eventStoredSessionId: 'stored-visible',
        fromActiveSource: false,
        selectedStoredSessionId: 'stored-visible',
        storedIdsShareLineage: (left, right) => left === right
      })
    ).toBe(false)
  })

  it('rejects an exact runtime-id collision from an inactive source', () => {
    expect(
      gatewayEventIsActive({
        activeRuntimeSessionId: 'runtime-visible',
        eventRuntimeSessionId: 'runtime-visible',
        eventStoredSessionId: 'stored-visible',
        fromActiveSource: false,
        selectedStoredSessionId: 'stored-visible',
        storedIdsShareLineage: (left, right) => left === right
      })
    ).toBe(false)
  })

  it('rejects a runtime mismatch without a durable conversation mapping', () => {
    expect(
      gatewayEventIsActive({
        activeRuntimeSessionId: 'runtime-visible',
        eventRuntimeSessionId: 'runtime-recovered',
        eventStoredSessionId: null,
        fromActiveSource: true,
        selectedStoredSessionId: 'stored-visible',
        storedIdsShareLineage: (left, right) => left === right
      })
    ).toBe(false)
  })

  it('accepts a durable event id at the lineage tip when the selected id is the root', () => {
    expect(
      gatewayEventIsActive({
        activeRuntimeSessionId: 'runtime-stale',
        eventRuntimeSessionId: 'runtime-current',
        eventStoredSessionId: 'stored-tip',
        fromActiveSource: true,
        selectedStoredSessionId: 'stored-root',
        storedIdsShareLineage
      })
    ).toBe(true)
  })

  it('rejects a durable event id from an unrelated lineage', () => {
    expect(
      gatewayEventIsActive({
        activeRuntimeSessionId: 'runtime-visible',
        eventRuntimeSessionId: 'runtime-background',
        eventStoredSessionId: 'stored-unrelated',
        fromActiveSource: true,
        selectedStoredSessionId: 'stored-root',
        storedIdsShareLineage
      })
    ).toBe(false)
  })
})
