import { Box, Text, useInput, useStdout } from '@hermes/ink'
import { useMemo, useState } from 'react'

import type { ActionRegistry } from '../app/actionRegistry.js'
import type { ActionContext, ActionGroup, RankedAction, TuiAction } from '../domain/actions.js'
import type { Theme } from '../theme.js'

import { listRowStyle } from './overlayPrimitives.js'

export interface PaletteState {
  query: string
  selectedId: null | string
}

export type PaletteEvent =
  | { direction: -1 | 1; type: 'move' }
  | { type: 'query'; value: string }
  | { type: 'select' }

export const paletteWidth = (preferred: number, maxWidth: number) =>
  Math.max(1, Math.min(Math.trunc(preferred), Math.trunc(maxWidth)))

export const paletteContentWidth = (frameWidth: number) => Math.max(1, Math.trunc(frameWidth) - 5)

export const paletteUsesFrame = (terminalRows: number) => Math.trunc(terminalRows) >= 12

export const paletteHeight = (terminalRows: number) => {
  const rows = Math.trunc(terminalRows)

  return paletteUsesFrame(rows) ? Math.max(4, Math.min(18, rows - 8)) : Math.max(1, rows - 5)
}

export interface PaletteViewportRow {
  row: RankedAction
  showGroup: boolean
  showReason: boolean
}

export function paletteRowsForViewport(
  rows: RankedAction[],
  selectedId: null | string,
  maxLines: number
): PaletteViewportRow[] {
  const budget = Math.max(1, Math.trunc(maxLines))

  const collect = (start: number) => {
    const visible: PaletteViewportRow[] = []
    let lines = 0
    let previousGroup: ActionGroup | null = null

    for (let index = start; index < rows.length && lines < budget; index += 1) {
      const row = rows[index]

      if (!row) {
        continue
      }

      const groupChanged = row.action.group !== previousGroup
      const remaining = budget - lines

      if (groupChanged && visible.length > 0 && remaining < 2) {
        break
      }

      const showGroup = groupChanged && remaining >= 2

      if (showGroup) {
        lines += 1
      }

      lines += 1

      const showReason = row.availability.status === 'disabled' && lines < budget

      if (showReason) {
        lines += 1
      }

      visible.push({ row, showGroup, showReason })
      previousGroup = row.action.group
    }

    return visible
  }

  const fromTop = collect(0)

  if (!selectedId || fromTop.some(item => item.row.action.id === selectedId)) {
    return fromTop
  }

  const selectedIndex = rows.findIndex(row => row.action.id === selectedId)

  if (selectedIndex < 0) {
    return fromTop
  }

  let selectedViewport = collect(selectedIndex)

  for (let start = selectedIndex - 1; start >= 0; start -= 1) {
    const candidate = collect(start)

    if (!candidate.some(item => item.row.action.id === selectedId)) {
      break
    }

    selectedViewport = candidate
  }

  return selectedViewport
}

export const isPaletteCloseKey = (ch: string, key: { ctrl: boolean; escape: boolean }) =>
  key.escape || (key.ctrl && ch.toLowerCase() === 'c')

const paletteGraphemeSegmenter = new Intl.Segmenter(undefined, { granularity: 'grapheme' })

export function removeLastPaletteGrapheme(query: string) {
  let lastSegmentStart = 0

  for (const { index } of paletteGraphemeSegmenter.segment(query)) {
    lastSegmentStart = index
  }

  return query.slice(0, lastSegmentStart)
}

export function runPaletteSelection(
  state: PaletteState,
  registry: ActionRegistry,
  context: ActionContext,
  onRun: (action: TuiAction) => boolean
): boolean {
  const selected = registry.search(state.query, context).find(row => row.action.id === state.selectedId)

  if (selected?.availability.status !== 'enabled') {
    return false
  }

  return onRun(selected.action)
}

export function reducePaletteState(
  state: PaletteState,
  event: PaletteEvent,
  registry: ActionRegistry,
  context: ActionContext
): PaletteState {
  const query = event.type === 'query' ? event.value : state.query
  const rows = registry.search(query, context)

  if (!rows.length) {
    return { query, selectedId: null }
  }

  const currentIndex = rows.findIndex(row => row.action.id === state.selectedId)

  if (event.type === 'move') {
    const base = currentIndex < 0 ? 0 : currentIndex
    const next = (base + event.direction + rows.length) % rows.length

    return { query, selectedId: rows[next]?.action.id ?? null }
  }

  return {
    query,
    selectedId: currentIndex >= 0 ? state.selectedId : (rows[0]?.action.id ?? null)
  }
}

const GROUP_LABELS: Record<ActionGroup, string> = {
  agents: 'Agents',
  capabilities: 'Capabilities',
  context: 'Context',
  'model-profile': 'Model & profile',
  session: 'Session',
  suggested: 'Suggested',
  system: 'System',
  view: 'View'
}

export interface CommandPaletteProps {
  context: ActionContext
  initialQuery?: string
  maxWidth: number
  onClose: () => void
  onRun: (action: TuiAction) => boolean
  registry: ActionRegistry
  t: Theme
}

export function CommandPalette({
  context,
  initialQuery = '',
  maxWidth,
  onClose,
  onRun,
  registry,
  t
}: CommandPaletteProps) {
  const { stdout } = useStdout()
  const initialRows = useMemo(() => registry.search(initialQuery, context), [context, initialQuery, registry])

  const [state, setState] = useState<PaletteState>({
    query: initialQuery,
    selectedId: initialRows[0]?.action.id ?? null
  })

  const rows = registry.search(state.query, context)
  const width = paletteWidth(72, maxWidth)
  const height = paletteHeight(stdout?.rows ?? 24)
  const actionRows = Math.max(0, height - 3)
  const visibleRows = actionRows > 0 ? paletteRowsForViewport(rows, state.selectedId, actionRows) : []

  const update = (event: PaletteEvent) => setState(current => reducePaletteState(current, event, registry, context))

  useInput((ch, key) => {
    if (isPaletteCloseKey(ch, key)) {
      return onClose()
    }

    if (key.upArrow || key.downArrow) {
      return update({ direction: key.upArrow ? -1 : 1, type: 'move' })
    }

    if (key.return) {
      if (runPaletteSelection(state, registry, context, onRun)) {
        onClose()
      }

      return
    }

    if (key.backspace || key.delete) {
      return update({ type: 'query', value: removeLastPaletteGrapheme(state.query) })
    }

    if (ch && !key.ctrl && !key.meta && !key.tab) {
      update({ type: 'query', value: state.query + ch })
    }
  })

  return (
    <Box backgroundColor={t.color.statusBg} flexDirection="column" height={height} paddingX={width > 24 ? 1 : 0} width={width}>
      <Box justifyContent="space-between" width="100%">
        <Text bold color={t.color.primary} wrap="truncate-end">
          Actions
        </Text>
        <Text color={t.color.muted}>esc</Text>
      </Box>
      {height >= 2 ? (
        <Text color={state.query ? t.color.text : t.color.muted} wrap="truncate-end">
          {state.query || 'Search…'}
        </Text>
      ) : null}
      {visibleRows.length ? (
        visibleRows.map(({ row, showGroup, showReason }) => {
          const active = row.action.id === state.selectedId
          const style = listRowStyle(t, active)
          const disabledReason = row.availability.status === 'disabled' ? row.availability.reason : null

          return (
            <Box flexDirection="column" key={row.action.id} width="100%">
              {showGroup ? (
                <Text bold color={t.color.label} wrap="truncate-end">
                  {GROUP_LABELS[row.action.group]}
                </Text>
              ) : null}
              <Box backgroundColor={style.backgroundColor} justifyContent="space-between" width="100%">
                <Text bold={active} color={active ? style.color : disabledReason ? t.color.muted : t.color.text} wrap="truncate-end">
                  {active ? '▸ ' : '  '}
                  {row.action.title}
                </Text>
                {row.action.shortcut ? (
                  <Text color={active ? style.color : t.color.muted} wrap="truncate-end">
                    {row.action.shortcut}
                  </Text>
                ) : null}
              </Box>
              {showReason && disabledReason ? (
                <Text color={t.color.muted} wrap="truncate-end">
                  {'  '}
                  {disabledReason}
                </Text>
              ) : null}
            </Box>
          )
        })
      ) : actionRows > 0 && !rows.length ? (
        <Text color={t.color.muted}>No matching actions</Text>
      ) : null}
      {height >= 3 ? (
        <Text color={t.color.muted} wrap="truncate-end">
          ↑↓ move · Enter run · Esc close
        </Text>
      ) : null}
    </Box>
  )
}
