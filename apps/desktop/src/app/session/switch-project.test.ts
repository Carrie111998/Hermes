import type { MutableRefObject } from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import { $missingProjectPaths, $recentProjects, isProjectMissing } from '@/store/recent-projects'

import { switchToProject } from './switch-project'

const sessionRef = (id: null | string): MutableRefObject<string | null> => ({ current: id })

describe('switchToProject', () => {
  beforeEach(() => {
    $recentProjects.set([])
    $missingProjectPaths.set([])
  })

  it('switches to an existing folder and records it as recent', async () => {
    const switched: string[] = []

    const result = await switchToProject({
      activeSessionIdRef: sessionRef('session-a'),
      changeSessionCwd: async cwd => void switched.push(cwd),
      path: '/Users/me/proj/',
      probeExists: async () => true
    })

    expect(result).toBe('switched')
    // Normalized before it ever reaches the cwd mutation.
    expect(switched).toEqual(['/Users/me/proj'])
    expect($recentProjects.get().map(entry => entry.path)).toEqual(['/Users/me/proj'])
  })

  it('refuses to re-anchor a session at a folder that no longer exists', async () => {
    const switched: string[] = []

    const result = await switchToProject({
      activeSessionIdRef: sessionRef('session-a'),
      changeSessionCwd: async cwd => void switched.push(cwd),
      path: '/deleted/project',
      probeExists: async () => false
    })

    expect(result).toBe('missing')
    expect(switched).toEqual([])
    expect(isProjectMissing('/deleted/project')).toBe(true)
    // A dead path must not enter the MRU.
    expect($recentProjects.get()).toEqual([])
  })

  it('aborts when focus moves to another session while the probe is in flight', async () => {
    // The regression this guards: the probe adds an await BEFORE the cwd write,
    // so a user switching chats mid-probe could otherwise have the stale intent
    // land on the NEW conversation — pointing that agent's terminal and file
    // tools at a project it was never opened for.
    const ref = sessionRef('session-a')
    const switched: string[] = []

    const result = await switchToProject({
      activeSessionIdRef: ref,
      changeSessionCwd: async cwd => void switched.push(cwd),
      path: '/Users/me/proj',
      probeExists: async () => {
        ref.current = 'session-b'

        return true
      }
    })

    expect(result).toBe('session-changed')
    expect(switched).toEqual([])
    expect($recentProjects.get()).toEqual([])
  })

  it('still switches when focus legitimately stays on the same session', async () => {
    const ref = sessionRef('session-a')
    const switched: string[] = []

    const result = await switchToProject({
      activeSessionIdRef: ref,
      changeSessionCwd: async cwd => void switched.push(cwd),
      path: '/Users/me/proj',
      probeExists: async () => true
    })

    expect(result).toBe('switched')
    expect(switched).toEqual(['/Users/me/proj'])
  })

  it('treats the new-chat case (no focused session) as switchable', async () => {
    const switched: string[] = []

    const result = await switchToProject({
      activeSessionIdRef: sessionRef(null),
      changeSessionCwd: async cwd => void switched.push(cwd),
      path: '/Users/me/proj',
      probeExists: async () => true
    })

    expect(result).toBe('switched')
    expect(switched).toEqual(['/Users/me/proj'])
  })

  it('rejects an unusable path without probing or mutating', async () => {
    let probed = false
    const switched: string[] = []

    const result = await switchToProject({
      activeSessionIdRef: sessionRef('session-a'),
      changeSessionCwd: async cwd => void switched.push(cwd),
      path: '   ',
      probeExists: async () => {
        probed = true

        return true
      }
    })

    expect(result).toBe('invalid')
    expect(probed).toBe(false)
    expect(switched).toEqual([])
  })

  it('does not record the project when the cwd change itself fails', async () => {
    await expect(
      switchToProject({
        activeSessionIdRef: sessionRef('session-a'),
        changeSessionCwd: async () => {
          throw new Error('gateway refused')
        },
        path: '/Users/me/proj',
        probeExists: async () => true
      })
    ).rejects.toThrow('gateway refused')

    expect($recentProjects.get()).toEqual([])
  })
})
