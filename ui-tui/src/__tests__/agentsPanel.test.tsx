import { PassThrough } from 'stream'

import { Box, renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { AgentsPanelView } from '../components/agentsPanel.js'
import type { AgentRow } from '../lib/agentRows.js'
import { PANEL_MAX_ROWS } from '../lib/agentRows.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

/** Render through real Ink and return the painted frame. Walking the returned
 * element tree instead (the old harness) hid every layout bug in this panel:
 * wrapping, over-tall frames and truncation are all Yoga's doing, not the
 * component's, so they only show up in the frame. */
const renderFrame = (element: React.ReactElement, columns = 72): string[] => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  // Ink writes one full frame per chunk, and unmount repaints, so the frames
  // must be kept apart — concatenating them doubles every line.
  const frames: string[] = []

  Object.assign(stdout, { columns, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    frames.push(chunk.toString())
  })

  const instance = renderSync(element, {
    patchConsole: false,
    stderr: stderr as NodeJS.WriteStream,
    stdin: stdin as NodeJS.ReadStream,
    stdout: stdout as NodeJS.WriteStream
  })

  instance.unmount()
  instance.cleanup()

  const painted = frames.filter(frame => stripAnsi(frame).trim() !== '').at(-1) ?? ''

  return stripAnsi(painted)
    .split('\n')
    .map(line => line.replace(/\s+$/, ''))
}

/** Frame minus the trailing blank rows the marginBottom and the writer add. */
const bodyLines = (lines: string[]): string[] => {
  const out = [...lines]

  while (out.length && out.at(-1) === '') {
    out.pop()
  }

  return out
}

/** Click handlers are props, not paint, so the two click tests still walk the
 * element tree — everything about layout goes through the frame instead. */
const textContent = (node: React.ReactNode): string => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return ''
  }

  if (typeof node === 'string' || typeof node === 'number') {
    return String(node)
  }

  if (Array.isArray(node)) {
    return node.map(textContent).join('')
  }

  if (React.isValidElement(node)) {
    return textContent(node.props.children)
  }

  return ''
}

const findClickableWithText = (node: React.ReactNode, needle: string): null | React.ReactElement => {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return null
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findClickableWithText(child, needle)

      if (found) {
        return found
      }
    }

    return null
  }

  if (!React.isValidElement(node)) {
    return null
  }

  if (typeof node.props.onClick === 'function' && textContent(node).includes(needle)) {
    return node
  }

  return findClickableWithText(node.props.children, needle)
}

const row = (over: Partial<AgentRow> = {}): AgentRow => ({
  detail: '',
  elapsedSeconds: null,
  goal: 'do a thing',
  id: '',
  key: 'k',
  name: '',
  resultReady: false,
  status: 'running',
  ...over
})

const rows = (n: number, over: Partial<AgentRow> = {}): AgentRow[] =>
  Array.from({ length: n }, (_, i) => row({ key: `k${i}`, ...over }))

describe('AgentsPanelView', () => {
  const base = { collapsed: false, cols: 72, done: 0, rows: [row()], running: 1, t: DEFAULT_THEME }

  const frame = (over: Partial<React.ComponentProps<typeof AgentsPanelView>> = {}, columns = 72) =>
    renderFrame(
      <Box flexDirection="column" width={columns}>
        <AgentsPanelView {...base} {...over} />
      </Box>,
      columns
    )

  it('renders the header with running/done counts', () => {
    const text = frame({ done: 1, running: 2 }).join('\n')

    expect(text).toContain('agents')
    expect(text).toContain('2 running')
    expect(text).toContain('1 done')
  })

  it('renders nothing when there are no rows (empty state)', () => {
    expect(AgentsPanelView({ ...base, rows: [] })).toBeNull()
    expect(bodyLines(frame({ rows: [] }))).toEqual([])
  })

  it('hides row bodies when collapsed but keeps the header', () => {
    const text = frame({ collapsed: true, rows: [row({ goal: 'secret goal' })] }).join('\n')

    expect(text).toContain('agents')
    expect(text).not.toContain('secret goal')
  })

  it('appends ⏎ to a result-ready row', () => {
    const text = frame({ rows: [row({ detail: 'result ready', resultReady: true, status: 'completed' })] }).join('\n')

    expect(text).toContain('result ready ⏎')
  })

  it('shows the @id so the user can see what @<id> takes', () => {
    const text = frame({ rows: [row({ id: 'b7c2' })] }).join('\n')

    expect(text).toContain('@b7c2')
  })

  it('exposes a clickable overlay affordance wired to onOpenTree', () => {
    const onOpenTree = vi.fn()
    const el = AgentsPanelView({ ...base, onOpenTree })

    const tree = findClickableWithText(el, '/agents')
    expect(tree).not.toBeNull()
    tree!.props.onClick()
    expect(onOpenTree).toHaveBeenCalledOnce()
  })

  it('does not advertise a ^a binding, because no such key handler exists', () => {
    expect(frame({ onOpenTree: () => {} }).join('\n')).not.toContain('^a')
  })

  it('toggles collapse when the header is clicked', () => {
    const onToggle = vi.fn()
    const el = AgentsPanelView({ ...base, onToggle })

    const header = findClickableWithText(el, 'agents')
    expect(header).not.toBeNull()
    header!.props.onClick()
    expect(onToggle).toHaveBeenCalled()
  })
})

describe('AgentsPanelView frame height', () => {
  const base = { collapsed: false, cols: 72, done: 0, running: 1, t: DEFAULT_THEME }

  const frame = (over: Partial<React.ComponentProps<typeof AgentsPanelView>>, columns = 72) =>
    renderFrame(
      <Box flexDirection="column" width={columns}>
        <AgentsPanelView {...base} rows={[row()]} {...over} />
      </Box>,
      columns
    )

  it('never paints more than one line per row (long goals truncate, not wrap)', () => {
    const lines = bodyLines(
      frame({ rows: [row({ detail: 'read_file', elapsedSeconds: 42, goal: 'x'.repeat(400), id: 'b7c2' })] })
    )

    // header + exactly one row line.
    expect(lines).toHaveLength(2)
    expect(lines.every(line => [...line].length <= 72)).toBe(true)
  })

  it('stays bounded at PANEL_MAX_ROWS rows plus the header', () => {
    const lines = bodyLines(frame({ rows: rows(PANEL_MAX_ROWS) }))

    expect(lines).toHaveLength(PANEL_MAX_ROWS + 1)
  })

  it('says how many agents it is not showing instead of silently dropping them', () => {
    const lines = bodyLines(frame({ hidden: 7, rows: rows(PANEL_MAX_ROWS), running: 12 }))

    expect(lines[0]).toContain('+7 more')
    expect(lines).toHaveLength(PANEL_MAX_ROWS + 1)
  })

  it('collapses to a single header line, giving the transcript every row back', () => {
    expect(bodyLines(frame({ collapsed: true, rows: rows(PANEL_MAX_ROWS) }))).toHaveLength(1)
  })

  it('stays within the frame on a narrow terminal', () => {
    for (const columns of [30, 40, 52, 72, 120]) {
      const lines = bodyLines(
        frame(
          {
            cols: columns,
            rows: [row({ detail: 'read_file', elapsedSeconds: 130, goal: 'map auth handshake edge cases', id: 'b7c2', name: 'fixer' })]
          },
          columns
        )
      )

      expect(lines).toHaveLength(2)
      expect(lines.every(line => [...line].length <= columns)).toBe(true)
    }
  })

  it('advertises @id steering in the header while something is running', () => {
    const lines = bodyLines(frame({ cols: 72, running: 2, rows: rows(2, { id: 'b7c2' }) }, 72))

    expect(lines[0]).toContain('@id steer')
    // The ids the hint refers to have to be on screen for it to mean anything.
    expect(lines[1]).toContain('@b7c2')
  })

  it('drops the steer hint when nothing is running', () => {
    const header = bodyLines(
      frame({ cols: 72, done: 1, running: 0, rows: [row({ status: 'completed' })] }, 72)
    )[0]!

    expect(header).not.toContain('@id steer')
  })

  it('drops optional header hints rather than letting them squeeze the counts', () => {
    // Yoga shrinks a sibling to fit an unbudgeted hint, so an over-long header
    // does not overflow — it silently eats the counts. Both hints have to give
    // way instead, widest-first, and the counts have to survive intact.
    for (const columns of [30, 36, 38, 39]) {
      const lines = bodyLines(
        frame({ cols: columns, onOpenTree: () => {}, running: 2, rows: rows(2) }, columns)
      )

      expect(lines[0]).toContain('2 running · 0 done')
      expect(lines[0]).not.toContain('@id steer')
      // One header line, two rows — a hint must never buy itself a wrap.
      expect(lines).toHaveLength(3)
      expect(lines.every(line => [...line].length <= columns)).toBe(true)
    }

    // /agents fits from 38; the steer hint needs the full 49.
    expect(bodyLines(frame({ cols: 38, onOpenTree: () => {}, running: 2 }, 38))[0]).toContain('/agents')
    expect(bodyLines(frame({ cols: 49, onOpenTree: () => {}, running: 2 }, 49))[0]).toContain('@id steer')
  })
})
