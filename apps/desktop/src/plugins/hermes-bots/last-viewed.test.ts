/**
 * Unread-since-last-viewed: the durable signal for headless / relay-driven
 * Bot Chat activity. First encounter seeds; a later last_active past the
 * persisted last-viewed is unseen; opening advances the watermark.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const { storageMock } = vi.hoisted(() => ({
  storageMock: { get: vi.fn(), set: vi.fn() }
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const { atom } = await import('nanostores')

  return { atom }
})

vi.mock('./shared', () => ({
  getPluginCtx: () => ({ storage: storageMock }),
  ID: 'hermes-bots'
}))

async function loadLastViewed() {
  vi.resetModules()

  return import('./last-viewed')
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('parseLastViewedMap', () => {
  it('keeps finite non-negative stamps and drops junk', async () => {
    const { parseLastViewedMap } = await loadLastViewed()

    expect(parseLastViewedMap(null)).toEqual({})
    expect(parseLastViewedMap(['x'])).toEqual({})
    expect(
      parseLastViewedMap({
        researcher: 5000,
        '': 1,
        bad: 'nope',
        scribe: '6000',
        neg: -3,
        empty: ''
      })
    ).toEqual({
      researcher: 5000,
      scribe: 6000
    })
  })
})

describe('botHasUnseenActivity', () => {
  it('is false on first encounter and true once last_active passes last-viewed', async () => {
    const { botHasUnseenActivity } = await loadLastViewed()

    expect(botHasUnseenActivity(6000, undefined)).toBe(false)
    expect(botHasUnseenActivity(0, 5000)).toBe(false)
    expect(botHasUnseenActivity(5000, 5000)).toBe(false)
    expect(botHasUnseenActivity(6000, 5000)).toBe(true)
  })
})

describe('hydrate and remember', () => {
  it('restores persisted stamps so a remount still sees unseen activity', async () => {
    const { botHasUnseenActivity, hydrateLastViewed, lastViewedByBot, $lastViewedHydrated } =
      await loadLastViewed()

    hydrateLastViewed({ researcher: 5000 })

    expect($lastViewedHydrated.get()).toBe(true)
    expect(lastViewedByBot.get('researcher')).toBe(5000)
    expect(botHasUnseenActivity(6000, lastViewedByBot.get('researcher'))).toBe(true)
  })

  it('advances last-viewed on open and persists the map', async () => {
    const { hydrateLastViewed, lastViewedByBot, rememberBotViewed } = await loadLastViewed()

    hydrateLastViewed({ researcher: 5000 })
    rememberBotViewed('researcher', 6000)

    expect(lastViewedByBot.get('researcher')).toBe(6000)
    expect(storageMock.set).toHaveBeenCalledWith('roster-last-viewed-v1', { researcher: 6000 })
  })

  it('does not move last-viewed backwards', async () => {
    const { hydrateLastViewed, lastViewedByBot, rememberBotViewed } = await loadLastViewed()

    hydrateLastViewed({ researcher: 8000 })
    rememberBotViewed('researcher', 6000)

    expect(lastViewedByBot.get('researcher')).toBe(8000)
    expect(storageMock.set).not.toHaveBeenCalled()
  })
})
