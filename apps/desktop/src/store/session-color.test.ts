import { afterEach, describe, expect, it } from 'vitest'

import { sessionIdentityKey } from '@/lib/session-identity'
import type { ProjectInfo, SessionInfo } from '@/types/hermes'

import { $projects } from './projects'
import { $sessions, sessionPinId } from './session'
import {
  $sessionColorById,
  $sessionColorOverrides,
  decodeSessionColorOverrides,
  encodeSessionColorOverrides,
  sessionColorFor,
  setSessionColorOverride
} from './session-color'

let nextId = 0

function makeSession(cwd: null | string, overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: false,
    cwd,
    ended_at: null,
    id: `s${nextId++}`,
    input_tokens: 0,
    is_active: false,
    last_active: 1_000,
    message_count: 1,
    model: 'claude',
    output_tokens: 0,
    preview: null,
    source: 'cli',
    started_at: 1_000,
    title: null,
    tool_call_count: 0,
    ...overrides
  }
}

function makeProject(id: string, folders: string[], color: null | string): ProjectInfo {
  return {
    archived: false,
    board_slug: null,
    color,
    created_at: 0,
    description: null,
    folders: folders.map((path, i) => ({ added_at: 0, is_primary: i === 0, label: null, path })),
    icon: null,
    id,
    name: id,
    primary_path: folders[0] ?? null,
    slug: id
  }
}

afterEach(() => {
  $sessions.set([])
  $projects.set([])
  $sessionColorOverrides.set({})
})

describe('$sessionColorById', () => {
  it('maps each session under a colored project to that color, keyed by live id', () => {
    const a = makeSession('/www/app/src', { git_repo_root: '/www/app' })
    const b = makeSession('/other/place')

    $projects.set([makeProject('p_app', ['/www/app'], '#4a9eff')])
    $sessions.set([a, b])

    const map = $sessionColorById.get()

    expect(map[sessionIdentityKey(a.id, a.profile)]).toBe('#4a9eff')
    // Sessions with no colored project are absent (a sparse map, not null-filled).
    expect(sessionIdentityKey(b.id, b.profile) in map).toBe(false)
  })

  it('omits a session whose project has no color', () => {
    const a = makeSession('/www/app', { git_repo_root: '/www/app' })

    $projects.set([makeProject('p_app', ['/www/app'], null)])
    $sessions.set([a])

    expect(sessionIdentityKey(a.id, a.profile) in $sessionColorById.get()).toBe(false)
  })

  it('recomputes when the projects list changes (color applied later)', () => {
    const a = makeSession('/www/app', { git_repo_root: '/www/app' })

    $sessions.set([a])
    $projects.set([makeProject('p_app', ['/www/app'], null)])
    expect($sessionColorById.get()[sessionIdentityKey(a.id, a.profile)]).toBeUndefined()

    $projects.set([makeProject('p_app', ['/www/app'], '#7bc86c')])
    expect($sessionColorById.get()[sessionIdentityKey(a.id, a.profile)]).toBe('#7bc86c')
  })
})

describe('$sessionColorOverrides', () => {
  it('migrates legacy raw ids as opaque default-profile values', () => {
    const sentinelLookingId = 'alpha\u0000shared'

    expect(decodeSessionColorOverrides(JSON.stringify({ [sentinelLookingId]: '#aa0000', shared: '#00bb00' }))).toEqual({
      [sessionIdentityKey(sentinelLookingId, 'default')]: '#aa0000',
      [sessionIdentityKey('shared', 'default')]: '#00bb00'
    })
  })

  it('round-trips opaque ids without colliding with an owner-qualified identity', () => {
    const sentinelLookingId = 'alpha\u0000shared'

    const overrides = {
      [sessionIdentityKey(sentinelLookingId, 'default')]: '#aa0000',
      [sessionIdentityKey('shared', 'alpha')]: '#00bb00'
    }

    expect(decodeSessionColorOverrides(encodeSessionColorOverrides(overrides))).toEqual(overrides)
  })

  it('an override wins over the inherited project color', () => {
    const a = makeSession('/www/app', { git_repo_root: '/www/app' })

    $projects.set([makeProject('p_app', ['/www/app'], '#4a9eff')])
    $sessions.set([a])
    setSessionColorOverride(sessionPinId(a), '#ff0000')

    expect($sessionColorById.get()[sessionIdentityKey(a.id, a.profile)]).toBe('#ff0000')
  })

  it('clearing an override falls back to the project color', () => {
    const a = makeSession('/www/app', { git_repo_root: '/www/app' })

    $projects.set([makeProject('p_app', ['/www/app'], '#4a9eff')])
    $sessions.set([a])

    setSessionColorOverride(sessionPinId(a), '#ff0000')
    expect($sessionColorById.get()[sessionIdentityKey(a.id, a.profile)]).toBe('#ff0000')

    setSessionColorOverride(sessionPinId(a), null)
    expect($sessionColorById.get()[sessionIdentityKey(a.id, a.profile)]).toBe('#4a9eff')
  })

  it('keys on the durable lineage id so a color survives compression', () => {
    // The live id rotates on auto-compression; the override is stored against the
    // lineage root, so the continuation tip still resolves to the same color.
    const root = makeSession('/x', { id: 'root' })
    const tip = makeSession('/x', { id: 'tip', _lineage_root_id: 'root' })

    setSessionColorOverride(sessionPinId(root), '#abcdef')

    $sessions.set([tip])
    expect($sessionColorById.get()[sessionIdentityKey('tip', tip.profile)]).toBe('#abcdef')
  })
})

describe('sessionColorFor', () => {
  it('reads a single session through the same shared map', () => {
    const a = makeSession('/www/app', { git_repo_root: '/www/app' })

    $projects.set([makeProject('p_app', ['/www/app'], '#5865f2')])
    $sessions.set([a])

    expect(sessionColorFor(a)).toBe('#5865f2')
  })

  it('isolates colors when profiles share a stored session id', () => {
    const alpha = makeSession('/alpha', { id: 'shared', profile: 'alpha' })
    const beta = makeSession('/beta', { id: 'shared', profile: 'beta' })

    setSessionColorOverride(sessionPinId(alpha), '#aa0000')
    setSessionColorOverride(sessionPinId(beta), '#0000bb')
    $sessions.set([alpha, beta])

    expect(sessionColorFor(alpha)).toBe('#aa0000')
    expect(sessionColorFor(beta)).toBe('#0000bb')
  })

  it('returns undefined for a null/absent session', () => {
    expect(sessionColorFor(null)).toBeUndefined()
    expect(sessionColorFor(undefined)).toBeUndefined()
  })
})
