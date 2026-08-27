import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $composerNewChatGeneration, onComposerDraftSyncRequest } from '@/store/composer'
import { $primarySessionOwnerIntent, $selectedStoredSessionId } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'
import type * as WindowsStore from '@/store/windows'

import { markActiveComposer } from '../chat/composer/focus'
import { sessionTileComposerTarget } from '../chat/session-tile'

import { hudTargetSession, useHudHandoff, useReportHudSession } from './handoff'

const windowKind = vi.hoisted(() => ({ isHud: true }))

vi.mock('@/store/windows', async importOriginal => {
  const actual = await importOriginal<typeof WindowsStore>()

  return { ...actual, isHudWindow: () => windowKind.isHud }
})

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop
const setSession = vi.fn()

let emitHudChanged:
  | ((state: {
      newChatGeneration: number | string | null
      open: boolean
      ownerRoute: null | { connectionId: string; profile: string }
      sessionId: string | null
    }) => void)
  | null = null

const onChanged = vi.fn(callback => {
  emitHudChanged = callback

  return vi.fn()
})

beforeEach(() => {
  setSession.mockClear()
  onChanged.mockClear()
  emitHudChanged = null
  windowKind.isHud = true
  desktopWindow.hermesDesktop = { hud: { onChanged, setSession } } as unknown as Window['hermesDesktop']
  $primarySessionOwnerIntent.set(null)
  $selectedStoredSessionId.set(null)
  $sessionTiles.set([])
  $composerNewChatGeneration.set('66666666-6666-4666-8666-666666666666')
})

afterEach(() => {
  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('hudTargetSession', () => {
  it('returns the exact owner of the focused duplicate tile composer', () => {
    const ownerA = { connectionId: 'source-a', profile: 'worker' }
    const ownerB = { connectionId: 'source-b', profile: 'worker' }
    $sessionTiles.set([
      { ownerRoute: ownerA, storedSessionId: 'shared' },
      { ownerRoute: ownerB, storedSessionId: 'shared' }
    ])
    markActiveComposer(sessionTileComposerTarget('shared', ownerB))

    expect(hudTargetSession()).toEqual({ ownerRoute: ownerB, sessionId: 'shared' })
  })
})

describe('useReportHudSession', () => {
  it('re-reports when the HUD advances to another New Chat generation', () => {
    renderHook(() => useReportHudSession())

    expect(setSession).toHaveBeenLastCalledWith({
      newChatGeneration: '66666666-6666-4666-8666-666666666666',
      ownerRoute: null,
      sessionId: null
    })

    act(() => $composerNewChatGeneration.set('77777777-7777-4777-8777-777777777777'))

    expect(setSession).toHaveBeenLastCalledWith({
      newChatGeneration: '77777777-7777-4777-8777-777777777777',
      ownerRoute: null,
      sessionId: null
    })
    expect(setSession).toHaveBeenCalledTimes(2)
  })
})

describe('useHudHandoff', () => {
  it('adopts the HUD New Chat in main routing before hydrating its exact generation', async () => {
    const generation = '99999999-9999-4999-8999-999999999999'
    const staleOwner = { connectionId: 'source-a', profile: 'worker' }
    const navigate = vi.fn()

    const hydrationStates: Array<{
      generation: number | string
      ownerIntent: unknown
      selectedSessionId: string | null
    }> = []

    windowKind.isHud = false
    $selectedStoredSessionId.set('stored-selected-before-hud')
    $primarySessionOwnerIntent.set({ ownerRoute: staleOwner, storedSessionId: 'stored-selected-before-hud' })

    const offDraftSync = onComposerDraftSyncRequest(() => {
      hydrationStates.push({
        generation: $composerNewChatGeneration.get(),
        ownerIntent: $primarySessionOwnerIntent.get(),
        selectedSessionId: $selectedStoredSessionId.get()
      })
    })

    renderHook(() => useHudHandoff({ navigate, resumeSession: vi.fn() }))

    await act(async () => {
      emitHudChanged?.({
        newChatGeneration: generation,
        open: false,
        ownerRoute: null,
        sessionId: null
      })
      await Promise.resolve()
    })

    expect($composerNewChatGeneration.get()).toBe(generation)
    expect($selectedStoredSessionId.get()).toBeNull()
    expect($primarySessionOwnerIntent.get()).toBeNull()
    expect(navigate).toHaveBeenCalledWith('/')
    expect(hydrationStates).toEqual([{ generation, ownerIntent: null, selectedSessionId: null }])
    offDraftSync()
  })

  it('re-resumes a stored HUD session through its exact connection owner', () => {
    const ownerRoute = { connectionId: 'source-b', profile: 'worker' }
    const resumeSession = vi.fn()
    windowKind.isHud = false
    $selectedStoredSessionId.set('shared')
    $primarySessionOwnerIntent.set({ ownerRoute, storedSessionId: 'shared' })
    renderHook(() => useHudHandoff({ navigate: vi.fn(), resumeSession }))

    act(() =>
      emitHudChanged?.({
        newChatGeneration: null,
        open: false,
        ownerRoute,
        sessionId: 'shared'
      })
    )

    expect(resumeSession).toHaveBeenCalledWith('shared', false, ownerRoute)
  })

  it('routes a duplicate raw id into main when the HUD owner differs from the selected owner', () => {
    const ownerA = { connectionId: 'source-a', profile: 'worker' }
    const ownerB = { connectionId: 'source-b', profile: 'worker' }
    const navigate = vi.fn()
    const resumeSession = vi.fn()

    windowKind.isHud = false
    $selectedStoredSessionId.set('shared')
    $primarySessionOwnerIntent.set({ ownerRoute: ownerA, storedSessionId: 'shared' })
    renderHook(() => useHudHandoff({ navigate, resumeSession }))

    act(() =>
      emitHudChanged?.({
        newChatGeneration: null,
        open: false,
        ownerRoute: ownerB,
        sessionId: 'shared'
      })
    )

    expect($primarySessionOwnerIntent.get()).toEqual({ ownerRoute: ownerB, storedSessionId: 'shared' })
    expect(navigate).toHaveBeenCalledWith('/shared')
    expect(resumeSession).not.toHaveBeenCalled()
  })

  it('preserves the ownerless same-session handoff shortcut for legacy HUD reports', () => {
    const ownerA = { connectionId: 'source-a', profile: 'worker' }
    const navigate = vi.fn()
    const resumeSession = vi.fn()

    windowKind.isHud = false
    $selectedStoredSessionId.set('shared')
    $primarySessionOwnerIntent.set({ ownerRoute: ownerA, storedSessionId: 'shared' })
    renderHook(() => useHudHandoff({ navigate, resumeSession }))

    act(() =>
      emitHudChanged?.({ newChatGeneration: null, open: false, ownerRoute: null, sessionId: 'shared' })
    )

    expect($primarySessionOwnerIntent.get()).toEqual({ ownerRoute: ownerA, storedSessionId: 'shared' })
    expect(navigate).not.toHaveBeenCalled()
    expect(resumeSession).toHaveBeenCalledWith('shared', false, undefined)
  })
})
