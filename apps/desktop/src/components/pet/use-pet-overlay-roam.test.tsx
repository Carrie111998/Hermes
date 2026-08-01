import { act, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { motionSet, roamDirSet } = vi.hoisted(() => ({ motionSet: vi.fn(), roamDirSet: vi.fn() }))

vi.mock('@/store/pet', () => ({
  $petMotion: { set: motionSet },
  $petRoamDir: { set: roamDirSet }
}))

import { desktopRoamRange, usePetOverlayRoam } from './use-pet-overlay-roam'

let root: Root | null = null
let container: HTMLDivElement | null = null
let getWorkArea: ReturnType<typeof vi.fn>
let setBounds: ReturnType<typeof vi.fn>

function render(ui: ReactNode) {
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)

  act(() => {
    root!.render(ui)
  })
}

function cleanup() {
  if (root) {
    act(() => {
      root!.unmount()
    })
  }

  container?.remove()
  root = null
  container = null
}

function installRaf() {
  let nextId = 1
  const frames = new Map<number, FrameRequestCallback>()

  Object.defineProperty(window, 'requestAnimationFrame', {
    configurable: true,
    value: vi.fn((callback: FrameRequestCallback) => {
      const id = nextId++
      frames.set(id, callback)

      return id
    })
  })
  Object.defineProperty(window, 'cancelAnimationFrame', {
    configurable: true,
    value: vi.fn((id: number) => frames.delete(id))
  })

  return {
    pending: () => frames.size,
    runNext: (now: number) => {
      const next = frames.entries().next().value

      if (!next) {
        throw new Error('No pending RAF')
      }

      const [id, callback] = next
      frames.delete(id)
      callback(now)
    }
  }
}

function RoamHarness() {
  usePetOverlayRoam({ enabled: true, isInteracting: () => false, loopMs: 1000, petW: 64 })

  return null
}

describe('usePetOverlayRoam', () => {
  beforeEach(() => {
    ;(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
    vi.useFakeTimers()
    motionSet.mockClear()
    roamDirSet.mockClear()
    vi.spyOn(document, 'hasFocus').mockReturnValue(false)
    vi.spyOn(Math, 'random').mockReturnValue(0.9)
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    Object.defineProperty(window, 'screenX', { configurable: true, value: 100 })
    Object.defineProperty(window, 'screenY', { configurable: true, value: 200 })
    Object.defineProperty(window, 'outerWidth', { configurable: true, value: 240 })
    Object.defineProperty(window, 'outerHeight', { configurable: true, value: 300 })
    getWorkArea = vi.fn().mockResolvedValue({ height: 900, width: 1000, x: 0, y: 0 })
    setBounds = vi.fn()
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { petOverlay: { getWorkArea, setBounds } }
    })
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.restoreAllMocks()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('keeps the full overlay inside a secondary display work area', () => {
    expect(desktopRoamRange({ height: 900, width: 1200, x: -1200, y: 0 }, 240)).toEqual({
      left: -1200,
      right: -240
    })
  })

  it('walks horizontally at the user-selected desktop height', async () => {
    const raf = installRaf()

    render(<RoamHarness />)
    expect(raf.pending()).toBe(0)

    await act(async () => {
      vi.advanceTimersByTime(1300)
      await Promise.resolve()
    })

    expect(getWorkArea).toHaveBeenCalledTimes(1)
    expect(raf.pending()).toBe(1)

    act(() => {
      raf.runNext(1350)
    })

    expect(setBounds).toHaveBeenCalledWith({
      height: 300,
      width: 240,
      x: expect.any(Number),
      y: 200
    })
    expect(setBounds.mock.calls[0]?.[0].x).toBeLessThan(100)
    expect(roamDirSet).toHaveBeenCalledWith(-1)
  })
})
