import { describe, expect, it, vi } from 'vitest'

import {
  type EnumeratedWindow,
  enumerateViaMacJxa,
  enumerateWindowsWithFallback,
  enumerationFailureNote,
  type MacJxaCommand,
  parseMacWindowEnumeration,
  pickWindowBelow,
  readWindowBelow
} from './window-below'

const win = (pid: number, x = 0, y = 0, width = 800, height = 600, app = `app-${pid}`): EnumeratedWindow => ({
  app,
  bounds: { x, y, width, height },
  id: pid * 10,
  pid,
  title: `${app} window`
})

const SELF_PID = 42
const SELF_BOUNDS = { x: 100, y: 100, width: 800, height: 600 }

const MAC_WINDOW_JSON = JSON.stringify([
  {
    app: 'Google Chrome',
    bounds: { x: 20, y: 30, width: 1440, height: 900 },
    id: 123,
    pid: 456,
    title: 'Inbox'
  }
])

describe('pickWindowBelow', () => {
  it('picks the first overlapping window behind ours in z-order', () => {
    const chrome = win(1, 120, 120)
    const spotify = win(2, 130, 130)

    const { below, frontmost } = pickWindowBelow([win(SELF_PID, 100, 100), chrome, spotify], SELF_PID, SELF_BOUNDS)

    expect(below).toBe(chrome)
    expect(frontmost).toBe(chrome)
  })

  it('skips windows behind ours that do not overlap', () => {
    const elsewhere = win(1, 5000, 5000)
    const covered = win(2, 200, 200)

    const { below } = pickWindowBelow([win(SELF_PID, 100, 100), elsewhere, covered], SELF_PID, SELF_BOUNDS)

    expect(below).toBe(covered)
  })

  it('skips our own other windows (same pid) while walking down', () => {
    const secondHermesWindow = win(SELF_PID, 150, 150)
    const target = win(7, 160, 160)

    const { below } = pickWindowBelow([win(SELF_PID, 100, 100), secondHermesWindow, target], SELF_PID, SELF_BOUNDS)

    expect(below).toBe(target)
  })

  it('reports frontmost even when nothing overlaps', () => {
    const elsewhere = win(1, 5000, 5000)

    const { below, frontmost } = pickWindowBelow([win(SELF_PID, 100, 100), elsewhere], SELF_PID, SELF_BOUNDS)

    expect(below).toBeNull()
    expect(frontmost).toBe(elsewhere)
  })

  it('windows in front of ours are never "below", even overlapping', () => {
    const inFront = win(3, 110, 110)
    const behind = win(4, 120, 120)

    const { below, frontmost } = pickWindowBelow([inFront, win(SELF_PID, 100, 100), behind], SELF_PID, SELF_BOUNDS)

    expect(below).toBe(behind)
    expect(frontmost).toBe(inFront)
  })

  it('falls back to overlap-only when our own window is not in the list', () => {
    // macOS omits windows the enumerator cannot see; still answer usefully.
    const chrome = win(1, 120, 120)
    const { below } = pickWindowBelow([chrome], SELF_PID, SELF_BOUNDS)

    expect(below).toBe(chrome)
  })

  it('returns nulls for an empty enumeration', () => {
    const { below, frontmost } = pickWindowBelow([], SELF_PID, SELF_BOUNDS)

    expect(below).toBeNull()
    expect(frontmost).toBeNull()
  })

  it('edge-adjacent bounds do not count as overlap', () => {
    const adjacent = win(1, 900, 100) // starts exactly at our right edge

    const { below } = pickWindowBelow([win(SELF_PID, 100, 100), adjacent], SELF_PID, SELF_BOUNDS)

    expect(below).toBeNull()
  })
})

describe('enumerationFailureNote', () => {
  it('tells a Wayland user the session is the problem', () => {
    for (const env of [{ XDG_SESSION_TYPE: 'wayland' }, { WAYLAND_DISPLAY: 'wayland-0' }]) {
      expect(enumerationFailureNote('linux', env)).toMatch(/Wayland/)
    }
  })

  // Hyprland is asked over its own IPC, so the X11 tooling advice would be a
  // wrong turn — reaching here means the compositor didn't answer.
  it('points a Hyprland user at their compositor, not at xprop', () => {
    const note = enumerationFailureNote('linux', { HYPRLAND_INSTANCE_SIGNATURE: 'abc', XDG_SESSION_TYPE: 'wayland' })

    expect(note).toMatch(/Hyprland/)
    expect(note).not.toMatch(/xprop|X11\/Xorg/)
  })

  it('tells an X11 user which commands are missing', () => {
    const note = enumerationFailureNote('linux', { XDG_SESSION_TYPE: 'x11', DISPLAY: ':0' })

    expect(note).toMatch(/xprop/)
    expect(note).not.toMatch(/Wayland/)
  })

  // XWayland can still answer through xprop, so the fix is the tooling, not
  // switching session type.
  it('treats Wayland with an X display as X11', () => {
    const note = enumerationFailureNote('linux', { WAYLAND_DISPLAY: 'wayland-0', DISPLAY: ':0' })

    expect(note).toMatch(/xprop/)
    expect(note).not.toMatch(/Wayland/)
  })

  it('does not offer Linux advice on other platforms', () => {
    for (const platform of ['darwin', 'win32']) {
      const note = enumerationFailureNote(platform, {})

      expect(note).not.toMatch(/xprop|Wayland/)
      expect(note.length).toBeGreaterThan(0)
    }
  })
})

describe('enumerateWindowsWithFallback', () => {
  it('uses the built-in macOS fallback when get-windows cannot run', async () => {
    const chrome = win(1, 120, 120, 800, 600, 'Google Chrome')
    const primary = vi.fn(async () => null)
    const macFallback = vi.fn(async () => [chrome])

    const windows = await enumerateWindowsWithFallback(false, {
      platform: 'darwin',
      primary,
      macFallback
    })

    expect(windows).toEqual([chrome])
    expect(primary).toHaveBeenCalledWith(false)
    expect(macFallback).toHaveBeenCalledWith(false)
  })

  it('does not run the fallback when the primary enumerator succeeds', async () => {
    const safari = win(2, 120, 120, 800, 600, 'Safari')
    const primary = vi.fn(async () => [safari])
    const macFallback = vi.fn(async () => [win(3)])

    const windows = await enumerateWindowsWithFallback(true, {
      platform: 'darwin',
      primary,
      macFallback
    })

    expect(windows).toEqual([safari])
    expect(macFallback).not.toHaveBeenCalled()
  })

  it('treats an empty primary enumeration as a successful result', async () => {
    const primary = vi.fn(async () => [])
    const macFallback = vi.fn(async () => [win(3)])

    const windows = await enumerateWindowsWithFallback(false, {
      platform: 'darwin',
      primary,
      macFallback
    })

    expect(windows).toEqual([])
    expect(macFallback).not.toHaveBeenCalled()
  })

  it('propagates null when both macOS enumerators fail', async () => {
    const primary = vi.fn(async () => null)
    const macFallback = vi.fn(async () => null)

    const windows = await enumerateWindowsWithFallback(false, {
      platform: 'darwin',
      primary,
      macFallback
    })

    expect(windows).toBeNull()
    expect(macFallback).toHaveBeenCalledWith(false)
  })

  it('does not run the macOS fallback on another platform', async () => {
    const primary = vi.fn(async () => null)
    const macFallback = vi.fn(async () => [win(3)])

    const windows = await enumerateWindowsWithFallback(false, {
      platform: 'win32',
      primary,
      macFallback
    })

    expect(windows).toBeNull()
    expect(macFallback).not.toHaveBeenCalled()
  })
})

describe('enumerateViaMacJxa', () => {
  it('executes the built-in CoreGraphics script and parses its response', async () => {
    let command: MacJxaCommand | null = null

    const execute = vi.fn(async (nextCommand: MacJxaCommand) => {
      command = nextCommand

      return MAC_WINDOW_JSON
    })

    const windows = await enumerateViaMacJxa(false, execute)

    expect(command).toEqual({
      args: ['-l', 'JavaScript', '-e', expect.stringContaining('CGWindowListCopyWindowInfo')],
      file: '/usr/bin/osascript',
      options: { encoding: 'utf8', maxBuffer: 2 * 1024 * 1024, timeout: 3000 }
    })
    expect(windows?.[0]).toMatchObject({ app: 'Google Chrome', title: '' })
  })

  it('fails closed when the built-in subprocess rejects', async () => {
    const execute = vi.fn(async () => {
      throw new Error('osascript unavailable')
    })

    await expect(enumerateViaMacJxa(false, execute)).resolves.toBeNull()
  })
})

describe('parseMacWindowEnumeration', () => {
  it('normalizes the CoreGraphics response', () => {
    expect(parseMacWindowEnumeration(MAC_WINDOW_JSON, true)).toEqual([
      {
        app: 'Google Chrome',
        bounds: { x: 20, y: 30, width: 1440, height: 900 },
        id: 123,
        pid: 456,
        title: 'Inbox'
      }
    ])
  })

  it('drops titles unless Screen Recording was already granted', () => {
    expect(parseMacWindowEnumeration(MAC_WINDOW_JSON, false)?.[0]?.title).toBe('')
  })

  it('fails closed on malformed helper output', () => {
    expect(parseMacWindowEnumeration('not json', true)).toBeNull()
    expect(parseMacWindowEnumeration('{}', true)).toBeNull()
  })
})

describe('readWindowBelow', () => {
  it('uses the fallback-aware enumerator when Hyprland is unavailable', async () => {
    const target = win(7, 120, 120, 800, 600, 'Google Chrome')
    const readHyprland = vi.fn(async () => null)
    const enumerate = vi.fn(async () => [win(SELF_PID, 100, 100), target])

    const result = await readWindowBelow(SELF_PID, SELF_BOUNDS, false, { enumerate, readHyprland })

    expect(readHyprland).toHaveBeenCalledWith(SELF_PID)
    expect(enumerate).toHaveBeenCalledWith(false)
    expect('error' in result).toBe(false)

    if (!('error' in result)) {
      expect(result.window?.app).toBe('Google Chrome')
    }
  })
})
