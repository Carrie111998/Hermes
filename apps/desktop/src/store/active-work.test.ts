import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ClientSessionState } from '@/app/types'

import { $sessions } from './session'
import { clearAllSessionStates, publishSessionState } from './session-states'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const setActiveWork = vi.fn()

const busy = (storedSessionId: string, isBusy: boolean, storedSessionProfile = 'default') =>
  ({ busy: isBusy, needsInput: false, storedSessionId, storedSessionProfile }) as ClientSessionState

const session = (id: string, title: null | string, profile = 'default') =>
  ({ id, profile, title }) as (typeof $sessions.value)[number]

beforeAll(async () => {
  desktopWindow.hermesDesktop = { setActiveWork } as unknown as Window['hermesDesktop']
  // Subscribes at import time, so the bridge has to exist first.
  await import('./active-work')
})

beforeEach(() => {
  clearAllSessionStates()
  $sessions.set([])
  setActiveWork.mockClear()
})

describe('active work bridge', () => {
  it('reports a busy session by title', () => {
    $sessions.set([session('s1', 'Fix login'), session('s2', 'Idle chat')])
    publishSessionState('runtime-1', busy('s1', true))

    expect(setActiveWork).toHaveBeenLastCalledWith({ count: 1, titles: ['Fix login'] })
  })

  it('counts an untitled busy session without inventing a title', () => {
    $sessions.set([session('s1', null)])
    publishSessionState('runtime-1', busy('s1', true))

    expect(setActiveWork).toHaveBeenLastCalledWith({ count: 1, titles: [] })
  })

  it('reports the title from the exact owner when stored ids collide', () => {
    $sessions.set([session('shared', 'Alpha task', 'alpha'), session('shared', 'Beta task', 'beta')])
    publishSessionState('runtime-beta', busy('shared', true, 'beta'))

    expect(setActiveWork).toHaveBeenLastCalledWith({ count: 1, titles: ['Beta task'] })
  })

  it('drops back to nothing when the turn ends', () => {
    $sessions.set([session('s1', 'Fix login')])
    publishSessionState('runtime-1', busy('s1', true))
    publishSessionState('runtime-1', busy('s1', false))

    expect(setActiveWork).toHaveBeenLastCalledWith({ count: 0, titles: [] })
  })

  it('does not re-send an unchanged summary', () => {
    $sessions.set([session('s1', 'Fix login')])
    publishSessionState('runtime-1', busy('s1', true))
    setActiveWork.mockClear()

    $sessions.set([session('s1', 'Fix login'), session('s2', 'Something else')])

    expect(setActiveWork).not.toHaveBeenCalled()
  })
})
