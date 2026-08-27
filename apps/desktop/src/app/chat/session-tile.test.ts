import { afterEach, describe, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'
import { clearSessionDraft, stashSessionDraft } from '@/store/composer'
import { $activeGatewayProfile } from '@/store/profile'
import { $projectTree } from '@/store/projects'
import { $connection, $sessions, setPrimarySessionOwnerIntent } from '@/store/session'
import { $sessionTiles, sessionTileOwnerGeneration, sessionTilePaneId } from '@/store/session-states'

import {
  commitSessionTileResume,
  sessionTabDeleteOwnerRoute,
  sessionTileComposerTarget,
  sessionTileDraftScope,
  sessionTileResumeFailure,
  tileDragPayload,
  watchSessionTiles
} from './session-tile'

afterEach(() => {
  $activeGatewayProfile.set('default')
  $connection.set(null)
  $projectTree.set([])
  $sessions.set([])
  $sessionTiles.set([])
  setPrimarySessionOwnerIntent(null)
})

describe('exact-owner tile composer targets', () => {
  it('gives duplicate stored ids distinct focus/attachment/Esc/model/voice targets', () => {
    const ownerA = { connectionId: 'source-a', profile: 'default' }
    const ownerB = { connectionId: 'source-b', profile: 'default' }

    expect(sessionTileComposerTarget('shared-composer-id', ownerA)).not.toBe(
      sessionTileComposerTarget('shared-composer-id', ownerB)
    )
    expect(sessionTileComposerTarget('legacy-id')).toBe('tile:legacy-id')
  })
})

describe('tab delete owner routing', () => {
  it('captures the exact persisted owner of a tile tab', () => {
    const ownerRoute = { connectionId: 'source-b', mode: 'remote' as const, profile: 'worker' }
    $sessionTiles.set([{ ownerRoute, storedSessionId: 'duplicate-id' }])

    expect(sessionTabDeleteOwnerRoute('duplicate-id', 'session-tile:duplicate-id')).toEqual(ownerRoute)
  })

  it('captures the exact primary owner intent for the workspace tab', () => {
    const ownerRoute = { connectionId: 'source-a', mode: 'remote' as const, profile: 'worker' }
    setPrimarySessionOwnerIntent({ ownerRoute, storedSessionId: 'duplicate-id' })

    expect(sessionTabDeleteOwnerRoute('duplicate-id', 'workspace')).toEqual(ownerRoute)
  })
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
  it('commits a resume result only to its exact-owner duplicate tile', () => {
    const storedSessionId = 'shared-owner-id'
    const ownerA = { connectionId: 'source-a', profile: 'default' }
    const ownerB = { connectionId: 'source-b', profile: 'default' }

    $sessionTiles.set([
      { ownerRoute: ownerA, storedSessionId },
      { ownerRoute: ownerB, storedSessionId }
    ])
    const ownerGeneration = sessionTileOwnerGeneration(storedSessionId, ownerA)

    expect(commitSessionTileResume(storedSessionId, ownerGeneration, 'runtime-from-owner-a', ownerA)).toBe(true)
    expect($sessionTiles.get()[0]).toEqual(
      expect.objectContaining({ ownerRoute: ownerA, runtimeId: 'runtime-from-owner-a' })
    )
    expect($sessionTiles.get()[1]).toEqual(expect.objectContaining({ ownerRoute: ownerB }))
    expect($sessionTiles.get()[1]?.runtimeId).toBeUndefined()
  })
})

describe('exact-owner tile panes', () => {
  it('registers separate panes for duplicate stored ids on different owners', () => {
    const ownerA = { connectionId: 'source-a', profile: 'default' }
    const ownerB = { connectionId: 'source-b', profile: 'default' }

    $sessionTiles.set([
      { ownerRoute: ownerA, storedSessionId: 'shared-pane-id' },
      { ownerRoute: ownerB, storedSessionId: 'shared-pane-id' }
    ])
    watchSessionTiles()

    const paneIds = registry
      .getArea('panes')
      .map(entry => entry.id)
      .filter(id => id.includes('shared-pane-id'))

    expect(paneIds).toEqual([sessionTilePaneId('shared-pane-id', ownerA), sessionTilePaneId('shared-pane-id', ownerB)])
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
