import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('@hermes/ink', async importOriginal => {
  const mod = await importOriginal<Record<string, unknown>>()

  return { ...mod, useInput: () => {} }
})

import { ActionRegistry } from '../app/actionRegistry.js'
import { FloatBox } from '../components/appChrome.js'
import {
  CommandPalette,
  isPaletteCloseKey,
  paletteContentWidth,
  paletteHeight,
  paletteRowsForViewport,
  paletteUsesFrame,
  paletteWidth,
  reducePaletteState,
  removeLastPaletteGrapheme,
  runPaletteSelection
} from '../components/commandPalette.js'
import type { ActionContext, TuiAction } from '../domain/actions.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const context: ActionContext = {
  busy: false,
  dashboard: false,
  dispatchSlash: vi.fn(),
  hasSession: true
}

const actions: TuiAction[] = [
  {
    aliases: ['resume'],
    availability: () => ({ status: 'enabled' }),
    description: 'Browse conversations',
    group: 'session',
    id: 'sessions',
    run: vi.fn(),
    shortcut: 'Ctrl+X',
    title: 'Switch session'
  },
  {
    availability: () => ({ reason: 'Wait for the active turn to finish', status: 'disabled' }),
    group: 'model-profile',
    id: 'model',
    run: vi.fn(),
    title: 'Switch model'
  }
]

const render = (maxWidth: number, registry = new ActionRegistry(actions), rows = 20, framed = false) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: maxWidth, isTTY: false, rows })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const palette = React.createElement(CommandPalette, {
    context,
    maxWidth,
    onClose: vi.fn(),
    onRun: vi.fn().mockReturnValue(true),
    registry,
    t: DEFAULT_THEME
  })

  const instance = renderSync(
    framed ? React.createElement(FloatBox, { children: palette, color: DEFAULT_THEME.color.border }) : palette,
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  const rendered = stripAnsi(output)

  instance.unmount()
  instance.cleanup()

  return rendered
}

describe('command palette behavior', () => {
  it('filters by aliases and keeps selection on the same action when possible', () => {
    const registry = new ActionRegistry(actions)
    const initial = { query: '', selectedId: 'sessions' }
    const filtered = reducePaletteState(initial, { type: 'query', value: 'res' }, registry, context)

    expect(filtered).toEqual({ query: 'res', selectedId: 'sessions' })
  })

  it('moves with arrows and Enter only runs enabled actions', () => {
    const registry = new ActionRegistry(actions)

    const selectedDisabled = reducePaletteState(
      { query: '', selectedId: 'sessions' },
      { direction: 1, type: 'move' },
      registry,
      context
    )

    const onRun = vi.fn().mockReturnValue(true)

    expect(selectedDisabled.selectedId).toBe('model')
    expect(runPaletteSelection(selectedDisabled, registry, context, onRun)).toBe(false)
    expect(onRun).not.toHaveBeenCalled()
    expect(runPaletteSelection({ query: '', selectedId: 'sessions' }, registry, context, onRun)).toBe(true)
    expect(onRun).toHaveBeenCalledWith(actions[0])
  })

  it('keeps the palette open when live action context rejects execution', () => {
    const registry = new ActionRegistry(actions)
    const onRun = vi.fn().mockReturnValue(false)

    expect(runPaletteSelection({ query: '', selectedId: 'sessions' }, registry, context, onRun)).toBe(false)
    expect(onRun).toHaveBeenCalledWith(actions[0])
  })

  it('closes on Esc and Ctrl+C', () => {
    expect(isPaletteCloseKey('', { ctrl: false, escape: true })).toBe(true)
    expect(isPaletteCloseKey('c', { ctrl: true, escape: false })).toBe(true)
    expect(isPaletteCloseKey('x', { ctrl: true, escape: false })).toBe(false)
  })

  it('deletes one complete grapheme from Unicode queries', () => {
    expect(removeLastPaletteGrapheme('model 🧑🏽‍💻')).toBe('model ')
    expect(removeLastPaletteGrapheme('e\u0301')).toBe('')
    expect(removeLastPaletteGrapheme('')).toBe('')
  })

  it('renders shortcuts and disabled reasons', () => {
    const output = render(80)

    expect(output).toContain('Ctrl+X')
    expect(output).toContain('Wait for the active turn to finish')
  })

  it('uses one subtle overlay border instead of a competing double frame', () => {
    const output = render(80, new ActionRegistry(actions), 20, true)

    expect(output).toContain('╭')
    expect(output).not.toContain('╔')
  })

  it('reserves frame, content padding, and terminal guard column', () => {
    expect(paletteContentWidth(48)).toBe(43)
    expect(paletteContentWidth(5)).toBe(1)
  })

  it('reserves the five-row composer while staying stable during filtering', () => {
    expect(paletteHeight(24)).toBe(16)
    expect(paletteHeight(12)).toBe(4)

    for (const rows of [24, 12]) {
      const height = paletteHeight(rows)
      const visible = paletteRowsForViewport(new ActionRegistry(actions).search('', context), 'sessions', height - 3)

      const contentRows = visible.reduce(
        (total, item) => total + 1 + Number(item.showGroup) + Number(item.showReason),
        0
      )

      expect(contentRows).toBeLessThanOrEqual(Math.max(1, height - 3))
      expect(height + 5).toBeLessThanOrEqual(rows)
    }
  })

  it('drops the decorative frame before colliding with the composer in very short terminals', () => {
    expect(paletteUsesFrame(12)).toBe(true)
    expect(paletteUsesFrame(11)).toBe(false)

    for (let rows = 6; rows <= 24; rows += 1) {
      const frameRows = paletteUsesFrame(rows) ? 3 : 0

      expect(paletteHeight(rows) + frameRows + 5).toBeLessThanOrEqual(rows)
    }
  })

  it('keeps the selected action visible when the list must fit a short palette', () => {
    const rows = new ActionRegistry(actions).search('', context)
    const visible = paletteRowsForViewport(rows, 'model', 2)

    expect(visible.map(item => item.row.action.id)).toEqual(['model'])
    expect(visible[0]?.showGroup).toBe(true)
  })

  it('does not render an action beneath the previous group when its label cannot fit', () => {
    const rows = new ActionRegistry(actions).search('', context)
    const visible = paletteRowsForViewport(rows, 'sessions', 3)

    expect(visible.map(item => item.row.action.id)).toEqual(['sessions'])
  })

  it('backfills preceding actions when selection moves beyond the first viewport', () => {
    const preceding: TuiAction = { ...actions[0]!, id: 'new', title: 'New session' }
    const rows = new ActionRegistry([preceding, ...actions]).search('', context)
    const visible = paletteRowsForViewport(rows, 'model', 4)

    expect(visible.map(item => item.row.action.id)).toEqual(['sessions', 'model'])
  })

  it('remains bounded at narrow widths', () => {
    expect(paletteWidth(80, 40)).toBe(40)
    expect(paletteWidth(80, 18)).toBe(18)
    const narrow = render(18)

    expect(narrow).toContain('Wait for the ac…')
    expect(narrow).not.toContain('Wait for the active turn to finish')
  })
})
