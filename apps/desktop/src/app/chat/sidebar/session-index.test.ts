import { describe, expect, it } from 'vitest'

import { createSessionPinKey } from '@/store/session-pin-key'
import type { SessionInfo } from '@/types/hermes'

import { buildSessionByPinKey } from './session-index'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

describe('buildSessionByPinKey', () => {
  it('resolves a pin from every slice the sidebar fetches', () => {
    // The contract that matters: a pin is looked up in this map no matter
    // which slice owns the row. Messaging is the one that regressed — a
    // pinned session is filtered out of its own section, so a miss here
    // removes it from the sidebar entirely rather than just misplacing it.
    const index = buildSessionByPinKey([row('recent')], [row('cron_job_1')], [row('telegram_42')])

    for (const id of ['recent', 'cron_job_1', 'telegram_42']) {
      expect(index.get(createSessionPinKey('default', id))?.id).toBe(id)
    }
  })

  it('resolves a pin stored on the pre-compression lineage root', () => {
    const index = buildSessionByPinKey([], [], [row('tip', { _lineage_root_id: 'root' })])

    // Both identities point at the one live row.
    expect(index.get(createSessionPinKey('default', 'root'))?.id).toBe('tip')
    expect(index.get(createSessionPinKey('default', 'tip'))?.id).toBe('tip')
  })

  it('lets a recents row win a direct id collision', () => {
    const index = buildSessionByPinKey(
      [row('dupe', { title: 'from recents' })],
      [],
      [row('dupe', { title: 'from messaging' })]
    )

    expect(index.get(createSessionPinKey('default', 'dupe'))?.title).toBe('from recents')
  })

  it('does not let a lineage alias clobber a real row under that id', () => {
    // 'root' is a live session in its own right AND another row's lineage
    // root; the real row must win so the pin opens the right conversation.
    const index = buildSessionByPinKey([row('root')], [], [row('tip', { _lineage_root_id: 'root' })])

    expect(index.get(createSessionPinKey('default', 'root'))?.id).toBe('root')
  })

  it('keeps cloned ids independently addressable by profile', () => {
    const index = buildSessionByPinKey(
      [
        row('shared', { pinned: false, profile: 'default', title: 'default copy' }),
        row('shared', { pinned: true, profile: 'work', title: 'work copy' })
      ],
      [],
      []
    )

    expect(index.get(createSessionPinKey('default', 'shared'))?.title).toBe('default copy')
    expect(index.get(createSessionPinKey('work', 'shared'))?.title).toBe('work copy')
    expect(index.has('shared')).toBe(false)
  })
})
