import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

const patch = vi.fn<(id: string, unread: boolean, profile?: null | string) => Promise<{ ok: boolean }>>(() =>
  Promise.resolve({ ok: true })
)

vi.mock('@/hermes', () => ({
  // The store only needs the REST mutation; keep the mock minimal.
  setApiRequestProfile: () => {},
  setSessionUnreadRemote: (id: string, unread: boolean, profile?: null | string) => patch(id, unread, profile)
}))

import { $sessions } from '@/store/session'

import { $unreadWriteGuard, clearUnreadOnOpen, markSessionUnread, watchUnreadWriteGuard } from './session-unread-remote'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

beforeEach(() => {
  $sessions.set([])
  $unreadWriteGuard.set(new Map())
  patch.mockClear()
})

afterEach(() => {
  $sessions.set([])
  $unreadWriteGuard.set(new Map())
})

describe('markSessionUnread', () => {
  it('optimistically paints the row, then PATCHes with the owning profile', async () => {
    $sessions.set([row('a', { profile: 'work', unread: false })])

    await markSessionUnread('a', true)

    expect(patch).toHaveBeenCalledWith('a', true, 'work')
    expect($sessions.get().find(s => s.id === 'a')?.unread).toBe(true)
  })

  it('no-ops for a runtime-only session with no persisted row', async () => {
    await markSessionUnread('ghost', true)

    expect(patch).not.toHaveBeenCalled()
  })

  it('rolls back the row and rethrows when the PATCH fails', async () => {
    $sessions.set([row('a', { unread: false })])
    patch.mockImplementationOnce(() => Promise.reject(new Error('offline')))

    await expect(markSessionUnread('a', true)).rejects.toThrow('offline')

    // The backend kept the old value, so the optimistic flip is undone and
    // the guard is released (nothing to fence a page about).
    expect($sessions.get().find(s => s.id === 'a')?.unread).toBe(false)
    expect($unreadWriteGuard.get().has('a')).toBe(false)
  })
})

describe('clearUnreadOnOpen', () => {
  // Always stamps `last_read_at` on open, even when the backend already
  // believes the session is read: `SessionDB.session_unread()` returns
  // False for rows whose `last_read_at` is NULL, so without an unconditional
  // write the watermark is never seeded and the renderer-side gap keeps
  // painting green dots after every cold start.
  it('PATCHes read for an already-read session to seed the backend watermark', async () => {
    $sessions.set([row('a', { profile: 'p1', unread: false })])

    await clearUnreadOnOpen('a')

    expect(patch).toHaveBeenCalledWith('a', false, 'p1')
    expect($sessions.get().find(s => s.id === 'a')?.unread).toBe(false)
  })

  it('PATCHes read for an unread session, using its owning profile', async () => {
    $sessions.set([row('a', { profile: 'p2', unread: true })])

    await clearUnreadOnOpen('a')

    expect(patch).toHaveBeenCalledWith('a', false, 'p2')
    expect($sessions.get().find(s => s.id === 'a')?.unread).toBe(false)
  })

  // The `!row` early-return is what keeps a brand-new chat (no persisted
  // backend row yet) from issuing a doomed PATCH — guard against
  // regression.
  it('no-ops for a runtime-only session with no persisted row', async () => {
    $sessions.set([])

    await clearUnreadOnOpen('ghost')

    expect(patch).not.toHaveBeenCalled()
  })

  it('swallows a failed PATCH (the next honest refresh heals the dot)', async () => {
    $sessions.set([row('a', { unread: true })])
    patch.mockImplementationOnce(() => Promise.reject(new Error('offline')))

    await expect(clearUnreadOnOpen('a')).resolves.toBeUndefined()
  })
})

describe('watchUnreadWriteGuard', () => {
  it('drops a guard entry once a list page confirms the value we wrote', () => {
    watchUnreadWriteGuard()
    const guard = new Map<string, { at: number; value: boolean }>()
    guard.set('a', { at: Date.now(), value: true })
    $unreadWriteGuard.set(guard)

    // The server caught up and echoes our value back.
    $sessions.set([row('a', { unread: true })])

    expect($unreadWriteGuard.get().has('a')).toBe(false)
  })

  it('keeps the guard while a page contradicts a write still in flight', () => {
    watchUnreadWriteGuard()
    const guard = new Map<string, { at: number; value: boolean }>()
    guard.set('a', { at: Date.now(), value: true })
    $unreadWriteGuard.set(guard)

    // A list request issued before the PATCH still says read. Honouring it
    // would silently undo the mark the user just made.
    $sessions.set([row('a', { unread: false })])

    expect($unreadWriteGuard.get().has('a')).toBe(true)
  })
})
