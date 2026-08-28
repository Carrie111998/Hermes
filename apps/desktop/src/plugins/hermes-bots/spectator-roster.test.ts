import { afterEach, describe, expect, it, vi } from 'vitest'

import { isSpectatorMode, spectatorRoster } from './data'
import { spectatorSessionOpenPlan } from './bot-row'

describe('spectator roster', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    delete window.__HERMES_SPECTATOR__
    delete window.__HERMES_SPECTATOR_TOKEN__
    delete window.__HERMES_BASE_PATH__
  })

  it('loads and sanitizes profiles through the scoped read-only endpoint', async () => {
    window.__HERMES_SPECTATOR__ = true
    window.__HERMES_SPECTATOR_TOKEN__ = 'read-only-token'
    window.__HERMES_BASE_PATH__ = '/hermes'
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          profiles: [
            {
              name: ' default ',
              display_name: ' Hermes ',
              description: ' Primary ',
              model: ' gpt ',
              provider: ' openai ',
              gateway_running: 1,
              ignored_secret: 'must-not-propagate'
            },
            { name: '   ' }
          ]
        }),
        { status: 200 }
      )
    )

    expect(isSpectatorMode()).toBe(true)
    await expect(spectatorRoster()).resolves.toEqual({
      profiles: [
        {
          name: 'default',
          display_name: 'Hermes',
          description: 'Primary',
          model: 'gpt',
          provider: 'openai',
          gateway_running: true,
          ui_meta: undefined,
          has_avatar: false,
          preferred_session: undefined
        }
      ],
      spectator: true
    })
    expect(fetchMock).toHaveBeenCalledWith('/hermes/api/profiles?bot_roster=true', {
      headers: { 'X-Hermes-Spectator-Token': 'read-only-token' }
    })
  })

  it('fails closed without a spectator credential', async () => {
    window.__HERMES_SPECTATOR__ = true
    await expect(spectatorRoster()).rejects.toThrow('spectator credential unavailable')
  })

  it('opens only an existing persisted conversation without a profile mutation target', () => {
    expect(spectatorSessionOpenPlan(undefined)).toBeNull()
    expect(
      spectatorSessionOpenPlan({ id: 'stored-1', message_count: 2 } as never, 'default')
    ).toEqual({
      sessionId: 'stored-1',
      options: {
        intent: 'main',
        profile: 'default',
        awaitHydration: true,
        expectHistory: true,
        keepAllProfilesScope: false
      }
    })
  })
})
