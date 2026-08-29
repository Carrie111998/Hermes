import { describe, expect, it } from 'vitest'

import {
  compactRosterProfile,
  EMPTY_ROSTER_SNAPSHOT,
  normalizeRosterSnapshot,
  ROSTER_SNAPSHOT_MAX_CONNECTIONS,
  ROSTER_SNAPSHOT_MAX_ROWS,
  updateRosterSnapshot
} from './roster-snapshot'

describe('compactRosterProfile', () => {
  it('keeps bounded paint and routing fields without persisting chat identity or text', () => {
    const compact = compactRosterProfile({
      canonical_session: { id: 'must-not-persist', preview: 'private canonical preview' },
      connectionId: 'local',
      description: 'Drafts things',
      display_name: 'Writer',
      last_session: { id: 'also-no', preview: 'private recent preview' },
      name: 'writer',
      route: { connectionId: 'local', mode: 'local', profile: 'writer', targetProfile: 'writer' },
      ui_meta: { 'hermes-bots': { chat: 'legacy-session-pointer' } },
      worker_session: { last_active: 123 }
    })

    expect(compact).toEqual({
      connectionId: 'local',
      description: 'Drafts things',
      display_name: 'Writer',
      name: 'writer',
      route: { connectionId: 'local', mode: 'local', profile: 'writer', targetProfile: 'writer' }
    })
    expect(compact).not.toHaveProperty('canonical_session')
    expect(compact).not.toHaveProperty('last_session')
    expect(compact).not.toHaveProperty('ui_meta')
    expect(compact).not.toHaveProperty('worker_session')
  })

  it('bounds row text before persistence', () => {
    const compact = compactRosterProfile({ description: 'x'.repeat(2000), name: 'y'.repeat(100) })

    expect(compact?.name).toHaveLength(64)
    expect(compact?.description).toHaveLength(1024)
  })
})

describe('roster snapshot normalization', () => {
  it('rejects unknown versions and malformed entries', () => {
    expect(normalizeRosterSnapshot({ entries: { local: { profiles: [{ name: 'writer' }] } }, version: 2 })).toEqual(
      EMPTY_ROSTER_SNAPSHOT
    )
    expect(normalizeRosterSnapshot(null)).toEqual(EMPTY_ROSTER_SNAPSHOT)
  })

  it('bounds connections and rows while re-sanitizing stored data', () => {
    let snapshot = EMPTY_ROSTER_SNAPSHOT

    for (let index = 0; index < ROSTER_SNAPSHOT_MAX_CONNECTIONS + 3; index += 1) {
      snapshot = updateRosterSnapshot(
        snapshot,
        `connection-${index}`,
        Array.from({ length: ROSTER_SNAPSHOT_MAX_ROWS + 4 }, (_, row) => ({
          canonical_session: { id: `secret-${row}` },
          name: `bot-${row}`
        })),
        [],
        index + 1
      )
    }

    expect(Object.keys(snapshot.entries)).toHaveLength(ROSTER_SNAPSHOT_MAX_CONNECTIONS)
    expect(snapshot.entries['connection-10'].profiles).toHaveLength(ROSTER_SNAPSHOT_MAX_ROWS)
    expect(snapshot.entries).not.toHaveProperty('connection-0')

    const normalized = normalizeRosterSnapshot(snapshot)
    expect(normalized.entries['connection-10'].profiles[0]).not.toHaveProperty('canonical_session')
  })
})
