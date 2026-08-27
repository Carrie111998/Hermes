import { afterEach, describe, expect, it } from 'vitest'

import { clearSessionDraft, stashSessionDraft } from '@/store/composer'
import { $activeGatewayProfile } from '@/store/profile'
import { $projectTree } from '@/store/projects'
import { $connection, $sessions } from '@/store/session'
import {
  $sessionTiles,
  sessionTileOwnerGeneration,
  setSessionTileWorkspaceScope
} from '@/store/session-states'

import { commitSessionTileResume, sessionTileDraftScope, sessionTileResumeFailure, tileDragPayload } from './session-tile'

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

describe('tile resume owner generation', () => {
  it('rejects owner A resume result after the tile is re-homed to owner B', () => {
    const storedSessionId = 'shared-owner-id'
    const ownerA = { connectionId: 'source-a', profile: 'default' }
    const ownerB = { connectionId: 'source-b', profile: 'default' }

    $sessionTiles.set([{ ownerRoute: ownerA, storedSessionId }])
    const ownerGeneration = sessionTileOwnerGeneration(storedSessionId)

    expect(
      setSessionTileWorkspaceScope(storedSessionId, { ownerRoute: ownerB, workspaceMode: 'sessions' })
    ).toBe(true)
    expect(commitSessionTileResume(storedSessionId, ownerGeneration, 'runtime-from-owner-a')).toBe(false)
    expect($sessionTiles.get()[0]).toMatchObject({ ownerRoute: ownerB })
    expect($sessionTiles.get()[0]?.runtimeId).toBeUndefined()
  })
})

describe('qualified tile draft titles', () => {
  it('selects title metadata from the tile exact owner when duplicate rows exist', () => {
    const ownerRoute = { connectionId: 'source-b', mode: 'remote' as const, profile: 'worker' }

    $sessionTiles.set([{ ownerRoute, storedSessionId: 'duplicate-id' }])
    $sessions.set([
      {
        connection_id: 'source-a',
        id: 'duplicate-id',
        profile: 'worker',
        preview: 'owner A preview',
        title: 'Owner A title'
      },
      {
        connection_id: 'source-b',
        id: 'duplicate-id',
        profile: 'worker',
        preview: 'owner B preview',
        title: 'Owner B title'
      }
    ] as never)

    expect(tileDragPayload('duplicate-id')).toEqual({
      id: 'duplicate-id',
      profile: 'worker',
      title: 'Owner B title',
      workspaceScope: {
        ownerRoute,
        workspaceMode: 'sessions',
        workspaceOwnerKey: undefined,
        workspaceTabTitle: undefined
      }
    })
  })

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
      title: 'Exact owner draft title',
      workspaceScope: {
        ownerRoute,
        workspaceMode: 'sessions',
        workspaceOwnerKey: undefined,
        workspaceTabTitle: undefined
      }
    })

    clearSessionDraft(scope)
  })
})
