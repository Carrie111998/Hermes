import { afterEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'

import { $desktopActionTasks, buildRailTasks, desktopActionTaskKey, upsertDesktopActionTask } from './activity'

const running = {
  exit_code: null,
  lines: [],
  name: 'computer-use-grant',
  pid: 11,
  running: true
}

const done = {
  exit_code: 0,
  lines: [],
  name: 'other',
  pid: null,
  running: false
}

afterEach(() => {
  $desktopActionTasks.set({})
  setApiRequestConnection(null)
  vi.useRealTimers()
})

describe('desktop action task identity', () => {
  it('keys the same action name by connection and profile', () => {
    upsertDesktopActionTask(running, { connectionId: 'homelab', profile: 'default' })
    upsertDesktopActionTask({ ...running, pid: 22 }, { connectionId: 'local', profile: 'default' })

    const tasks = $desktopActionTasks.get()

    expect(tasks[desktopActionTaskKey('computer-use-grant', { connectionId: 'homelab', profile: 'default' })]?.status.pid).toBe(
      11
    )
    expect(tasks[desktopActionTaskKey('computer-use-grant', { connectionId: 'local', profile: 'default' })]?.status.pid).toBe(
      22
    )

    const rail = buildRailTasks([], [], null, tasks)

    expect(rail.map(task => task.id).sort()).toEqual([
      'action:homelab::default:computer-use-grant',
      'action:local::default:computer-use-grant'
    ])
  })

  it('prunes an abandoned running row after the completed TTL', () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    upsertDesktopActionTask(running, { connectionId: 'homelab', profile: 'default' })

    vi.setSystemTime(5 * 60 * 1000 + 1)
    upsertDesktopActionTask(done)

    expect($desktopActionTasks.get()[desktopActionTaskKey('computer-use-grant', { connectionId: 'homelab', profile: 'default' })]).toBeUndefined()
    expect($desktopActionTasks.get()[desktopActionTaskKey('other')]).toBeDefined()
  })

  it('does not attach an ambient remote default grant to a local pin', () => {
    setApiRequestConnection('homelab')
    upsertDesktopActionTask(running, 'default')

    expect($desktopActionTasks.get()[desktopActionTaskKey('computer-use-grant', 'default')]?.status.pid).toBe(11)
    expect(
      $desktopActionTasks.get()[desktopActionTaskKey('computer-use-grant', { connectionId: 'local', profile: 'default' })]
    ).toBeUndefined()
  })
})
