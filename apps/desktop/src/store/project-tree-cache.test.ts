import { beforeEach, describe, expect, it } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'

import {
  PROJECT_TREE_CACHE_STORAGE_KEY,
  projectTreeCacheScopeKey,
  readProjectTreeSnapshot,
  writeProjectTreeSnapshot
} from './project-tree-cache'

const project = (id: string): SidebarProjectTree =>
  ({ id, label: `Project ${id}`, path: `/${id}`, repos: [], sessionCount: 0 }) as SidebarProjectTree

beforeEach(() => {
  window.localStorage.clear()
})

describe('project tree cache', () => {
  it('round-trips only within the exact connection and profile scope', () => {
    const scope = { connectionId: 'connection-a', profile: 'profile-a' }

    writeProjectTreeSnapshot(scope, { activeId: 'alpha', projects: [project('alpha')] })

    expect(readProjectTreeSnapshot(scope)).toEqual({ activeId: 'alpha', projects: [project('alpha')] })
    expect(readProjectTreeSnapshot({ connectionId: 'connection-a', profile: 'profile-b' })).toBeNull()
    expect(readProjectTreeSnapshot({ connectionId: 'connection-b', profile: 'profile-a' })).toBeNull()
  })

  it('rejects malformed or expired persisted data', () => {
    window.localStorage.setItem(PROJECT_TREE_CACHE_STORAGE_KEY, '{broken')
    expect(readProjectTreeSnapshot({ connectionId: 'local', profile: 'default' })).toBeNull()

    const key = projectTreeCacheScopeKey({ connectionId: 'local', profile: 'default' })
    window.localStorage.setItem(
      PROJECT_TREE_CACHE_STORAGE_KEY,
      JSON.stringify({
        [key]: {
          activeId: null,
          projects: [project('stale')],
          savedAt: Date.now() - 15 * 24 * 60 * 60 * 1000
        }
      })
    )

    expect(readProjectTreeSnapshot({ connectionId: 'local', profile: 'default' })).toBeNull()
  })
})
