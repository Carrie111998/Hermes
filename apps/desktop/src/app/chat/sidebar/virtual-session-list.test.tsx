import type * as ReactVirtual from '@tanstack/react-virtual'
import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import type { SidebarListRow } from '@/lib/session-date-groups'

import { VirtualSessionList } from './virtual-session-list'

type EstimateSize = (index: number) => number

let lastEstimateSize: EstimateSize | undefined
let lastGap: number | undefined

vi.mock('@tanstack/react-virtual', async importOriginal => {
  const actual = await importOriginal<typeof ReactVirtual>()

  return {
    ...actual,
    useVirtualizer: (opts: Parameters<typeof actual.useVirtualizer>[0]) => {
      lastEstimateSize = opts.estimateSize as EstimateSize
      lastGap = opts.gap

      return actual.useVirtualizer(opts)
    }
  }
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        dateDivider: {
          today: 'Today',
          yesterday: 'Yesterday'
        }
      }
    }
  })
}))

vi.mock('./chrome', () => ({
  SidebarDateDivider: () => <div data-testid="date-divider" />
}))

vi.mock('./session-row', () => ({
  SidebarSessionRow: () => <div data-testid="session-row" />
}))

function makeSession(id: string): SessionInfo {
  return {
    archived: false,
    cwd: '/tmp',
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: 1_000,
    message_count: 1,
    model: 'claude',
    output_tokens: 0,
    preview: 'preview',
    source: 'cli',
    started_at: 1_000,
    title: id,
    tool_call_count: 0
  }
}

const dividerRow: SidebarListRow = { key: 'today', kind: 'divider', label: 'Today' }
const sessionRow: SidebarListRow = { entry: { session: makeSession('sess-1') }, kind: 'session' }

function renderList(card = false) {
  return render(
    <VirtualSessionList
      activeSessionId={null}
      card={card}
      onArchiveSession={() => undefined}
      onDeleteSession={() => undefined}
      onResumeSession={() => undefined}
      onTogglePin={() => undefined}
      pinned={false}
      rows={[dividerRow, sessionRow]}
      sortable={false}
    />
  )
}

describe('VirtualSessionList size contract', () => {
  beforeEach(() => {
    lastEstimateSize = undefined
    lastGap = undefined
  })

  afterEach(cleanup)

  it('estimates divider / compact / card rows separately so a merge cannot drop one axis', () => {
    renderList(false)
    expect(lastEstimateSize?.(0)).toBe(26)
    expect(lastEstimateSize?.(1)).toBe(28)

    cleanup()
    renderList(true)
    expect(lastEstimateSize?.(0)).toBe(26)
    expect(lastEstimateSize?.(1)).toBe(66)
  })

  it('disables native overflow anchoring and matches gap-px in the virtualizer', () => {
    const { container } = renderList(false)
    expect(container.firstElementChild?.className).toContain('[overflow-anchor:none]')
    expect(lastGap).toBe(1)
  })
})
