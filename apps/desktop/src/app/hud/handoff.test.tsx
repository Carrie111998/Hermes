import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $composerNewChatGeneration } from '@/store/composer'
import { $selectedStoredSessionId } from '@/store/session'
import type * as WindowsStore from '@/store/windows'

import { useHudHandoff, useReportHudSession } from './handoff'

const windowKind = vi.hoisted(() => ({ isHud: true }))

vi.mock('@/store/windows', async importOriginal => {
  const actual = await importOriginal<typeof WindowsStore>()

  return { ...actual, isHudWindow: () => windowKind.isHud }
})

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop
const setSession = vi.fn()

let emitHudChanged: ((state: {
  newChatGeneration: number | string | null
  open: boolean
  sessionId: string | null
}) => void) | null = null

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
  $selectedStoredSessionId.set(null)
  $composerNewChatGeneration.set('66666666-6666-4666-8666-666666666666')
})

afterEach(() => {
  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('useReportHudSession', () => {
  it('re-reports when the HUD advances to another New Chat generation', () => {
    renderHook(() => useReportHudSession())

    expect(setSession).toHaveBeenLastCalledWith({
      newChatGeneration: '66666666-6666-4666-8666-666666666666',
      sessionId: null
    })

    act(() => $composerNewChatGeneration.set('77777777-7777-4777-8777-777777777777'))

    expect(setSession).toHaveBeenLastCalledWith({
      newChatGeneration: '77777777-7777-4777-8777-777777777777',
      sessionId: null
    })
    expect(setSession).toHaveBeenCalledTimes(2)
  })
})

describe('useHudHandoff', () => {
  it('adopts the exact New Chat generation reported when the HUD closes', () => {
    windowKind.isHud = false
    renderHook(() => useHudHandoff({ navigate: vi.fn(), resumeSession: vi.fn() }))

    act(() =>
      emitHudChanged?.({
        newChatGeneration: '99999999-9999-4999-8999-999999999999',
        open: false,
        sessionId: null
      })
    )

    expect($composerNewChatGeneration.get()).toBe('99999999-9999-4999-8999-999999999999')
  })
})
