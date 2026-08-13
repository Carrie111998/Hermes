import type * as ReactVirtual from '@tanstack/react-virtual'
import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import type { SidebarListRow } from '@/lib/session-date-groups'

import { VirtualSessionList } from '../virtual-session-list'

type VirtualizerOpts = Parameters<typeof ReactVirtual.useVirtualizer>[0]

let lastOpts: undefined | VirtualizerOpts

vi.mock('@tanstack/react-virtual', async importOriginal => {
  const actual = await importOriginal<typeof ReactVirtual>()

  return {
    ...actual,
    useVirtualizer: (opts: VirtualizerOpts) => {
      lastOpts = opts

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

vi.mock('../chrome', () => ({
  SidebarDateDivider: () => <div data-testid="date-divider" />
}))

vi.mock('../session-row', () => ({
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

const baseProps = {
  activeSessionId: null,
  onArchiveSession: () => undefined,
  onDeleteSession: () => undefined,
  onResumeSession: () => undefined,
  onTogglePin: () => undefined,
  pinned: false,
  rows: [dividerRow, sessionRow],
  sortable: false
}

describe('VirtualSessionList shared-scroll contract', () => {
  beforeEach(() => {
    lastOpts = undefined
  })

  afterEach(cleanup)

  it('does not paint a nested vertical scroller when a shared scroll element is provided', () => {
    const host = {
      getBoundingClientRect: () => ({
        bottom: 0,
        height: 0,
        left: 0,
        right: 0,
        toJSON: () => ({}),
        top: 0,
        width: 0,
        x: 0,
        y: 0
      }),
      scrollTop: 0
    } as HTMLElement

    const getScrollElement = () => host

    const { container } = render(
      <VirtualSessionList {...baseProps} getScrollElement={getScrollElement} />
    )

    const rootClass = container.firstElementChild?.className ?? ''
    expect(rootClass).not.toMatch(/overflow-y-auto/)
    expect(rootClass).not.toMatch(/overscroll-contain/)
    expect(lastOpts?.getScrollElement?.()).toBe(host)
  })
})
