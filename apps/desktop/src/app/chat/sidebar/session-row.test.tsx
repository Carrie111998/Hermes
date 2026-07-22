import { cleanup, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { SidebarSessionRow } from './session-row'

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: false,
    cwd: '/home/user/projects/hermes-agent',
    ended_at: null,
    id: 'sidebar-source',
    input_tokens: 0,
    is_active: false,
    last_active: 1_000,
    message_count: 2,
    model: 'claude',
    output_tokens: 0,
    preview: 'Deliver this session to Codex',
    source: 'cli',
    started_at: 1_000,
    title: 'Sidebar delivery source',
    tool_call_count: 0,
    ...overrides
  }
}

function renderRow(session: SessionInfo) {
  return render(
    createElement(SidebarSessionRow, {
      isPinned: false,
      isSelected: false,
      isWorking: false,
      onArchive: () => {},
      onDelete: () => {},
      onPin: () => {},
      onResume: () => {},
      session
    })
  )
}

afterEach(cleanup)

describe('SidebarSessionRow Codex delivery badge', () => {
  it.each([
    ['pending', 'Pending', 'text-(--ui-accent)'],
    ['visible', 'Visible in Codex', 'text-(--ui-text-tertiary)'],
    ['retrying', 'Retrying', 'text-(--ui-accent)'],
    ['failed', 'Failed', 'text-destructive']
  ] as const)('renders %s with the existing semantic tone', (state, label, tone) => {
    renderRow(makeSession({ bridge_sidebar_state: state }))

    const badge = screen.getByText(label)
    expect(badge.getAttribute('aria-label')).toBe(`Codex sidebar: ${label}`)
    expect(badge.getAttribute('title')).toBe(`Codex sidebar: ${label}`)
    expect(badge.className).toContain(tone)
  })
})
