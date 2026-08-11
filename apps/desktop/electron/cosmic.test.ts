import { describe, expect, it } from 'vitest'

import { isCosmic, readCosmicWindows } from './cosmic'
import type { EnumeratedWindow } from './window-below'

const win = (pid: number, app = `app-${pid}`): EnumeratedWindow => ({
  app,
  bounds: { x: 0, y: 0, width: 800, height: 600 },
  id: pid * 10,
  pid,
  title: `${app} window`
})

describe('isCosmic', () => {
  it('detects COSMIC from XDG_CURRENT_DESKTOP', () => {
    expect(isCosmic({ XDG_CURRENT_DESKTOP: 'COSMIC' })).toBe(true)
  })

  it('detects COSMIC from XDG_SESSION_DESKTOP', () => {
    expect(isCosmic({ XDG_SESSION_DESKTOP: 'cosmic' })).toBe(true)
  })

  it('is case-insensitive', () => {
    expect(isCosmic({ XDG_CURRENT_DESKTOP: 'COSMIC' })).toBe(true)
    expect(isCosmic({ XDG_CURRENT_DESKTOP: 'pop-cosmic' })).toBe(true)
  })

  it('returns false on non-COSMIC sessions', () => {
    expect(isCosmic({ XDG_CURRENT_DESKTOP: 'GNOME' })).toBe(false)
    expect(isCosmic({ XDG_CURRENT_DESKTOP: 'Hyprland' })).toBe(false)
    expect(isCosmic({})).toBe(false)
  })
})

describe('readCosmicWindows', () => {
  it('returns null off COSMIC so the established path is untouched', async () => {
    const enumerate = () => Promise.resolve([win(1)])

    expect(await readCosmicWindows(42, true, { XDG_CURRENT_DESKTOP: 'GNOME' }, enumerate)).toBeNull()
    expect(await readCosmicWindows(42, true, {}, enumerate)).toBeNull()
  })

  it('delegates to the X11 enumerator on COSMIC', async () => {
    const enumerated: EnumeratedWindow[] = [win(1), win(2)]
    const enumerate = () => Promise.resolve(enumerated)

    const result = await readCosmicWindows(42, true, { XDG_CURRENT_DESKTOP: 'COSMIC' }, enumerate)

    expect(result).toBe(enumerated)
  })

  it('surfaces the enumerator returning null on native-Wayland COSMIC', async () => {
    const enumerate = () => Promise.resolve(null)

    expect(await readCosmicWindows(42, true, { XDG_CURRENT_DESKTOP: 'COSMIC' }, enumerate)).toBeNull()
  })
})
