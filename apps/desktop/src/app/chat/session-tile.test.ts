import { afterEach, describe, expect, it } from 'vitest'

import { clearSessionDraft, stashSessionDraft } from '@/store/composer'
import { $activeGatewayProfile } from '@/store/profile'
import { $projectTree } from '@/store/projects'
import { $connection, $sessions } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { sessionTileDraftScope, sessionTileResumeFailure, tileDragPayload } from './session-tile'

afterEach(() => {
  $activeGatewayProfile.set('default')
  $connection.set(null)
  $projectTree.set([])
  $sessions.set([])
  $sessionTiles.set([])
})

describe('sessionTileResumeFailure', () => {
  it('keeps a confirmed durable session retryable instead of repeating a stale 404', () => {
    expect(sessionTileResumeFailure('session not found', true, true)).toBe(
      'Session is still available — retry resuming it.'
    )
  })

  it('fails safe on an inconclusive durable lookup', () => {
    expect(sessionTileResumeFailure('404', false, true)).toBe('Session unavailable — you can retry resuming it.')
  })

  it('does not overwrite a tile that rebound while the lookup was pending', () => {
    expect(sessionTileResumeFailure('session not found', true, false)).toBeUndefined()
  })
})

describe('qualified tile draft titles', () => {
  it('uses the exact tile route when duplicate stored ids exist across owners', () => {
    const ownerRoute = { connectionId: 'source-b', mode: 'remote' as const, profile: 'default' }
    const scope = sessionTileDraftScope('duplicate-id', ownerRoute)

    $activeGatewayProfile.set('default')
    $connection.set({ connectionId: 'source-a', mode: 'remote' } as never)
    $sessionTiles.set([{ ownerRoute, storedSessionId: 'duplicate-id' }])
    stashSessionDraft(scope, 'Exact owner draft title', [])

    expect(tileDragPayload('duplicate-id')).toEqual({
      id: 'duplicate-id',
      profile: 'default',
      title: 'Exact owner draft title'
    })

    clearSessionDraft(scope)
  })
})
