import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { memo } from 'react'

import { $asyncDelegations } from '../app/delegationStore.js'
import { patchOverlayState } from '../app/overlayStore.js'
import { toggleAgentsCollapsed, useTurnSelector } from '../app/turnStore.js'
import { $uiState } from '../app/uiStore.js'
import { type AgentRow, buildAgentRows, fitAgentRow } from '../lib/agentRows.js'
import { statusGlyph } from '../lib/subagentGlyph.js'
import type { Theme } from '../theme.js'

/** Pure presentational panel — no store access, so it renders in tests as a
 * plain function call (mirrors StatusRule). Not memo-wrapped so it stays
 * directly callable; the connected LiveAgentsPanel below carries the memo. */
export function AgentsPanelView({
  collapsed,
  cols,
  done,
  hidden = 0,
  onOpenTree,
  onToggle,
  rows,
  running,
  t
}: {
  collapsed: boolean
  cols: number
  done: number
  hidden?: number
  onOpenTree?: () => void
  onToggle?: () => void
  rows: AgentRow[]
  running: number
  t: Theme
}) {
  if (!rows.length) {
    return null
  }

  // The `/agents` hint is nested inside the header Box, so a click on it
  // also bubbles into the header's collapse toggle — the overlay would
  // open over a panel that just collapsed itself. Stop it at the child.
  const openTree = onOpenTree
    ? (e?: { stopImmediatePropagation?: () => void }) => {
        e?.stopImmediatePropagation?.()
        onOpenTree()
      }
    : undefined

  // `@<id> text` is the only way to steer a running agent, and the ids are
  // right there in the rows — but nothing says what to do with them. Advertise
  // it in the header, and only when it would actually work (something is
  // running) and the line has room, since a wrapped header costs a permanent
  // terminal row exactly like a wrapped body row does.
  const arrow = collapsed ? '▸ ' : '▾ '
  const counts = ` · ${running} running · ${done} done${hidden > 0 ? ` · +${hidden} more` : ''}`
  const headerLen = arrow.length + 'agents'.length + counts.length
  const headerLabel = 'agents'.slice(0, Math.max(0, cols - arrow.length))
  const headerCounts = counts.slice(0, Math.max(0, cols - arrow.length - headerLabel.length))
  const STEER_HINT = '  @id steer'
  const TREE_HINT = '  /agents'
  // Both hints are optional chrome and the counts are not. Yoga shrinks
  // whatever it must to fit the row, so an unbudgeted hint doesn't overflow —
  // it silently eats the text next to it ("· 2 running · 0     /agents" at 36
  // columns). Budget them explicitly, widest-first, and let the counts keep
  // the line when neither fits.
  const showTree = Boolean(onOpenTree) && cols - headerLen >= TREE_HINT.length

  const showSteerHint =
    running > 0 && cols - headerLen - (showTree ? TREE_HINT.length : 0) >= STEER_HINT.length

  return (
    <Box flexDirection="column" marginBottom={1}>
      <Box onClick={onToggle}>
        {/* Truncate, don't wrap. The hint budget above keeps the OPTIONAL
            chrome off a short line, but the counts are not optional and on a
            narrow terminal even "▾ agents · 2 running · 0 done" outgrows the
            row — and a wrapped header costs a permanent terminal row exactly
            like a wrapped body row does. Rows already truncate (fitAgentRow);
            the header has to match, and it does it the same way: budget the
            segments left-to-right rather than trusting Yoga to shrink a Text
            that has its own nested Text children. */}
        <Text color={t.color.muted}>
          <Text color={t.color.accent}>{arrow.slice(0, Math.max(0, cols))}</Text>
          <Text bold color={t.color.text}>
            {headerLabel}
          </Text>
          <Text color={t.color.statusFg} dim>
            {headerCounts}
          </Text>
        </Text>
        {showSteerHint && (
          <Text color={t.color.muted} dim>
            {STEER_HINT}
          </Text>
        )}
        {showTree && (
          <Text color={t.color.muted} dim onClick={openTree}>
            {TREE_HINT}
          </Text>
        )}
      </Box>

      {!collapsed && (
        <Box flexDirection="column" marginLeft={2}>
          {rows.map((row, i) => {
            const g = statusGlyph(row.status, t)
            // marginLeft={2} above eats two columns, so the row budget is cols - 2.
            const cell = fitAgentRow(row, i + 1, cols - 2)

            return (
              <Text color={t.color.statusFg} key={row.key} wrap="truncate">
                <Text color={t.color.muted}>{cell.index}</Text>
                <Text color={g.color}>{g.glyph} </Text>
                {cell.id && <Text color={t.color.accent}>{cell.id}</Text>}
                {cell.name && <Text color={t.color.text}>{cell.name}</Text>}
                {cell.goal && (
                  <Text color={t.color.statusFg} wrap="truncate-end">
                    {cell.goal}
                  </Text>
                )}
                {cell.elapsed && (
                  <Text color={t.color.muted} dim>
                    {cell.elapsed}
                  </Text>
                )}
                {cell.detail && (
                  <Text color={row.resultReady ? t.color.statusGood : t.color.muted} dim>
                    {cell.detail}
                    {cell.ready}
                  </Text>
                )}
              </Text>
            )
          })}
        </Box>
      )}
    </Box>
  )
}

/** Store-connected docked panel. Rides above the composer next to the todo
 * panel; renders nothing when there is no live or background agent. */
export const LiveAgentsPanel = memo(function LiveAgentsPanel({ cols }: { cols: number }) {
  const ui = useStore($uiState)
  const subagents = useTurnSelector(state => state.subagents)
  const collapsed = useTurnSelector(state => state.agentsCollapsed)
  const asyncDelegations = useStore($asyncDelegations)

  const { done, hidden, rows, running } = buildAgentRows(subagents, asyncDelegations, Date.now())

  return (
    <AgentsPanelView
      collapsed={collapsed}
      cols={cols}
      done={done}
      hidden={hidden}
      onOpenTree={() => patchOverlayState({ agents: true })}
      onToggle={toggleAgentsCollapsed}
      rows={rows}
      running={running}
      t={ui.theme}
    />
  )
})
