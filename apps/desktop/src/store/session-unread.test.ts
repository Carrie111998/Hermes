import { beforeEach, describe, expect, it } from 'vitest'

import { sessionRowIdentity } from '@/lib/session-row-identity'
import type { SessionInfo } from '@/types/hermes'

import { makeSessionInfo } from '../test/session-info'

import { $activeGatewayProfile } from './profile'
import {
  $cronSessions,
  $lastReadAtBySessionId,
  $messagingSessions,
  $selectedStoredSessionId,
  $sessions,
  $unreadFinishedSessionIds,
  markSessionRead,
  setPrimarySessionOwnerIntent,
  setSelectedStoredSessionId,
  setSessions
} from './session'
import { clearAllSessionStates } from './session-states'
import {
  $sessionSeenCounts,
  $unreadFinishedMarkers,
  ackStoredSessionId,
  forgetSessionUnread,
  markSessionUnreadFinished
} from './session-unread'

const session = (over: Partial<SessionInfo>): SessionInfo => makeSessionInfo({ id: 'live', ...over })

function resetAll() {
  clearAllSessionStates()
  // Lists first: their listeners recompute the unread atom, so wiping the
  // atom afterwards leaves a genuinely clean slate.
  $sessions.set([])
  $cronSessions.set([])
  $messagingSessions.set([])
  $selectedStoredSessionId.set(null)
  setPrimarySessionOwnerIntent(null)
  $activeGatewayProfile.set('default')
  $sessionSeenCounts.set({})
  $lastReadAtBySessionId.set({})
  $unreadFinishedMarkers.set({})
  $unreadFinishedSessionIds.set([])
}

describe('persisted unread (session-unread)', () => {
  beforeEach(resetAll)

  it('reconstructs the green dot from a seen watermark after a cold start', () => {
    // "Restart": persisted watermark says 3 messages were seen; the freshly
    // loaded list says the session now has 5 — it finished while we were away.
    $sessionSeenCounts.set({ default: { s1: 3 } })

    setSessions([session({ id: 's1', message_count: 5 })])

    expect($unreadFinishedSessionIds.get()).toEqual(['s1'])
  })

  it('seeds the watermark on first sight instead of painting every row green', () => {
    setSessions([session({ id: 's1', message_count: 5 })])

    expect($unreadFinishedSessionIds.get()).toEqual([])
    expect($sessionSeenCounts.get()).toEqual({ default: { s1: 5 } })
  })

  it('keeps a live-edge marker across a transient wipe (gateway/profile switch)', () => {
    setSessions([session({ id: 's1', message_count: 4 })])
    markSessionUnreadFinished('s1')
    expect($unreadFinishedSessionIds.get()).toEqual(['s1'])

    // Gateway switch: transient paint layer wiped, lists drained…
    setSessions([])
    $unreadFinishedSessionIds.set([])

    // …then the profile's lists load again: the persisted marker repaints.
    setSessions([session({ id: 's1', message_count: 4 })])

    expect($unreadFinishedSessionIds.get()).toEqual(['s1'])
  })

  it('acks watermark + marker when the user opens the session', () => {
    $sessionSeenCounts.set({ default: { s1: 3 } })
    setSessions([session({ id: 's1', message_count: 5 })])
    markSessionUnreadFinished('s1')

    setSelectedStoredSessionId('s1')

    expect($unreadFinishedSessionIds.get()).toEqual([])
    expect($unreadFinishedMarkers.get()).toEqual({})
    expect($sessionSeenCounts.get()).toEqual({ default: { s1: 5 } })

    // A later refresh with the same count stays read.
    setSessions([session({ id: 's1', message_count: 5 })])
    expect($unreadFinishedSessionIds.get()).toEqual([])
  })

  it('tracks the selected session’s count so on-screen activity never reads as unread', () => {
    $selectedStoredSessionId.set('s1')
    setSessions([session({ id: 's1', message_count: 2 })])

    // The turn completes while the user is looking at it.
    setSessions([session({ id: 's1', message_count: 7 })])

    expect($unreadFinishedSessionIds.get()).toEqual([])
    expect($sessionSeenCounts.get()).toEqual({ default: { s1: 7 } })

    // Navigating away must not retroactively flag it.
    setSelectedStoredSessionId(null)
    setSessions([session({ id: 's1', message_count: 7 })])
    expect($unreadFinishedSessionIds.get()).toEqual([])
  })

  it('follows auto-compression id rotation via the durable lineage id', () => {
    $sessionSeenCounts.set({ default: { root: 3 } })

    setSessions([session({ _lineage_root_id: 'root', id: 'tip-2', message_count: 6 })])

    expect($unreadFinishedSessionIds.get()).toEqual(['tip-2'])

    // Opening the rotated tip acks the lineage watermark.
    setSelectedStoredSessionId('tip-2')
    expect($sessionSeenCounts.get()).toEqual({ default: { root: 6 } })
  })

  it('reconstructs unread for cron rows too', () => {
    $sessionSeenCounts.set({ default: { cron1: 1 } })

    $cronSessions.set([session({ id: 'cron1', message_count: 3 })])

    expect($unreadFinishedSessionIds.get()).toEqual(['cron1'])
  })

  it('never paints messaging rows from count growth, but honors live-edge markers', () => {
    // First sight seeds…
    $messagingSessions.set([session({ id: 'tg1', message_count: 10 })])
    // …then inbound messages bump the count: NOT "finished — unread".
    $messagingSessions.set([session({ id: 'tg1', message_count: 14 })])

    expect($unreadFinishedSessionIds.get()).toEqual([])

    // A real background turn edge still marks it, durably.
    markSessionUnreadFinished('tg1')
    $unreadFinishedSessionIds.set([])
    $messagingSessions.set([session({ id: 'tg1', message_count: 14 })])

    expect($unreadFinishedSessionIds.get()).toEqual(['tg1'])
  })

  it('preserves a live-edge id whose row is not in any loaded list yet', () => {
    // A brand-new session's first turn isn't flushed to the sidebar list until
    // persisted — the flag must survive list refreshes in the meantime.
    markSessionUnreadFinished('fresh')

    setSessions([session({ id: 'other', message_count: 2 })])

    expect($unreadFinishedSessionIds.get()).toContain('fresh')
  })

  it('keeps same-id sessions in different profiles independent', () => {
    // Session ids are caller-supplied and every profile backend is its own
    // namespace, so "s1" in alpha and "s1" in beta are different sessions —
    // and cron/messaging lists always mix profiles into one array.
    $sessionSeenCounts.set({ alpha: { s1: 3 }, beta: { s1: 5 } })
    $unreadFinishedMarkers.set({ alpha: ['s1'] })

    setSessions([
      session({ id: 's1', message_count: 5, profile: 'beta' }),
      session({ id: 's1', message_count: 5, profile: 'alpha' })
    ])

    // Only alpha's row is past its watermark; beta's sits exactly on it.
    expect($unreadFinishedSessionIds.get()).toEqual(['s1'])
    expect($sessionSeenCounts.get()).toEqual({ alpha: { s1: 3 }, beta: { s1: 5 } })

    // Opening beta's row (the gateway is on beta) acks beta only.
    $activeGatewayProfile.set('beta')
    setSelectedStoredSessionId('s1')

    expect($sessionSeenCounts.get()).toEqual({ alpha: { s1: 3 }, beta: { s1: 5 } })
    // alpha's marker survives an ack of beta's identically-named session.
    expect($unreadFinishedMarkers.get()).toEqual({ alpha: ['s1'] })

    // A later refresh must not launder alpha's gap through beta's selection.
    setSessions([
      session({ id: 's1', message_count: 6, profile: 'beta' }),
      session({ id: 's1', message_count: 5, profile: 'alpha' })
    ])

    expect($sessionSeenCounts.get()).toEqual({ alpha: { s1: 3 }, beta: { s1: 6 } })
    expect($unreadFinishedMarkers.get()).toEqual({ alpha: ['s1'] })
  })

  it('marks only the exact owner when same-id twins share a profile', () => {
    const ownerA = session({ connection_id: 'source-a', id: 'shared', message_count: 5, profile: 'worker' })
    const ownerB = session({ connection_id: 'source-b', id: 'shared', message_count: 5, profile: 'worker' })
    const ownerRouteB = { connectionId: 'source-b', profile: 'worker' }

    setSessions([ownerA, ownerB])
    markSessionUnreadFinished('shared', ownerRouteB)

    expect($unreadFinishedSessionIds.get()).toEqual([sessionRowIdentity(ownerB)])
    expect($unreadFinishedMarkers.get()).toEqual({ worker: [sessionRowIdentity(ownerB)] })
  })

  it('rehydrates only the exact marked twin after transient state is lost', () => {
    const ownerA = session({ connection_id: 'source-a', id: 'shared', message_count: 5, profile: 'worker' })
    const ownerB = session({ connection_id: 'source-b', id: 'shared', message_count: 5, profile: 'worker' })

    setSessions([ownerA, ownerB])
    markSessionUnreadFinished('shared', { connectionId: 'source-b', profile: 'worker' })
    $unreadFinishedSessionIds.set([])

    setSessions([{ ...ownerA }, { ...ownerB }])

    expect($unreadFinishedSessionIds.get()).toEqual([sessionRowIdentity(ownerB)])
  })

  it('acks only the exact owner when same-id twins share a profile', () => {
    const ownerA = session({ connection_id: 'source-a', id: 'shared', message_count: 5, profile: 'worker' })
    const ownerB = session({ connection_id: 'source-b', id: 'shared', message_count: 5, profile: 'worker' })
    const ownerRouteA = { connectionId: 'source-a', profile: 'worker' }
    const ownerRouteB = { connectionId: 'source-b', profile: 'worker' }

    setSessions([ownerA, ownerB])
    markSessionUnreadFinished('shared', ownerRouteA)
    markSessionUnreadFinished('shared', ownerRouteB)

    ackStoredSessionId('shared', ownerRouteA)

    // The durable ack is exact-owner; the transient paint is cleared by
    // markSessionRead, which is exercised independently below.
    expect($unreadFinishedSessionIds.get()).toEqual([sessionRowIdentity(ownerA), sessionRowIdentity(ownerB)])
    expect($unreadFinishedMarkers.get()).toEqual({ worker: [sessionRowIdentity(ownerB)] })
  })

  it('keeps the unread twin intact while refreshing the selected owner', () => {
    const ownerA = session({ connection_id: 'source-a', id: 'shared', message_count: 5, profile: 'worker' })
    const ownerB = session({ connection_id: 'source-b', id: 'shared', message_count: 5, profile: 'worker' })
    const identityA = sessionRowIdentity(ownerA)
    const identityB = sessionRowIdentity(ownerB)
    const ownerRouteB = { connectionId: 'source-b', profile: 'worker' }

    setSessions([ownerA, ownerB])
    $sessionSeenCounts.set({ worker: { [identityA]: 3, [identityB]: 5 } })
    $unreadFinishedMarkers.set({ worker: [identityA] })
    setPrimarySessionOwnerIntent({ ownerRoute: ownerRouteB, storedSessionId: 'shared' })
    setSelectedStoredSessionId('shared')

    setSessions([{ ...ownerA }, { ...ownerB, message_count: 6 }])

    expect($sessionSeenCounts.get()).toEqual({ worker: { [identityA]: 3, [identityB]: 6 } })
    expect($unreadFinishedMarkers.get()).toEqual({ worker: [identityA] })
  })

  it('marks read only for the exact owner when same-id twins coexist', () => {
    const ownerA = session({ connection_id: 'source-a', id: 'shared', message_count: 5, profile: 'worker' })
    const ownerB = session({ connection_id: 'source-b', id: 'shared', message_count: 5, profile: 'worker' })
    const ownerRouteA = { connectionId: 'source-a', profile: 'worker' }

    setSessions([ownerA, ownerB])
    $unreadFinishedSessionIds.set([sessionRowIdentity(ownerA), sessionRowIdentity(ownerB)])

    markSessionRead('shared', ownerRouteA)

    expect($unreadFinishedSessionIds.get()).toEqual([sessionRowIdentity(ownerB)])
    expect(Object.keys($lastReadAtBySessionId.get())).toEqual([sessionRowIdentity(ownerA)])
  })

  it('files a live-edge marker with no loaded row under the active gateway profile', () => {
    $activeGatewayProfile.set('work')

    markSessionUnreadFinished('fresh')

    expect($unreadFinishedMarkers.get()).toEqual({ work: ['fresh'] })
    expect($unreadFinishedSessionIds.get()).toEqual(['fresh'])
  })

  it('forgets a deleted session’s persisted unread, per profile', () => {
    $sessionSeenCounts.set({ alpha: { root: 3, other: 1 }, beta: { root: 9 } })
    $unreadFinishedMarkers.set({ alpha: ['root', 'keep'], beta: ['root'] })
    $unreadFinishedSessionIds.set(['tip-2'])

    forgetSessionUnread(['tip-2', 'root', null, undefined], 'alpha')

    expect($sessionSeenCounts.get()).toEqual({ alpha: { other: 1 }, beta: { root: 9 } })
    expect($unreadFinishedMarkers.get()).toEqual({ alpha: ['keep'], beta: ['root'] })
    expect($unreadFinishedSessionIds.get()).toEqual([])
  })

  it('drops an emptied profile bucket instead of persisting an empty shell', () => {
    $sessionSeenCounts.set({ alpha: { s1: 3 } })
    $unreadFinishedMarkers.set({ alpha: ['s1'] })

    forgetSessionUnread(['s1'], 'alpha')

    expect($sessionSeenCounts.get()).toEqual({})
    expect($unreadFinishedMarkers.get()).toEqual({})
  })

  it('caps markers per profile, evicting the oldest', () => {
    for (let i = 0; i < 201; i += 1) {
      markSessionUnreadFinished(`s${i}`)
    }

    const markers = $unreadFinishedMarkers.get().default

    expect(markers).toHaveLength(200)
    expect(markers[0]).toBe('s1')
    expect(markers.at(-1)).toBe('s200')
  })
})
