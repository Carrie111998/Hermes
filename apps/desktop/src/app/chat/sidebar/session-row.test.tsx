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

describe('SidebarSessionRow bridge badges', () => {
  it('keeps the provider badge but omits the non-actionable catalog-only mirror state', () => {
    renderRow(
      makeSession({
        bridge_mirror_state: 'catalog_only',
        bridge_provider: 'claude',
        source: 'claude'
      })
    )

    expect(screen.getByText('Claude')).toBeTruthy()
    expect(screen.queryByText('Catalog only')).toBeNull()
  })

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

  it('tints Claude, Codex and Hermes provider badges with distinct hues', () => {
    const classes = (['claude', 'codex', 'hermes'] as const).map(provider => {
      const { unmount } = renderRow(makeSession({ bridge_provider: provider, source: provider }))
      const badge = screen.getByText(provider === 'claude' ? 'Claude' : provider === 'codex' ? 'Codex' : 'Hermes')
      const className = badge.className

      unmount()

      return className
    })

    expect(new Set(classes).size).toBe(3)

    for (const className of classes) {
      expect(className).not.toContain('text-(--ui-text-tertiary)')
    }
  })

  it('keeps an unrecognized provider badge on the neutral tint', () => {
    // The union type forbids this today, but the row renders whatever an older
    // or newer backend sends — the neutral fallback is the defensive contract.
    renderRow(makeSession({ bridge_provider: 'mystery' as SessionInfo['bridge_provider'], source: 'cli' }))

    expect(screen.getByText('Mystery').className).toContain('text-(--ui-text-tertiary)')
  })

  it('tints the driver badge with the same per-harness hue', () => {
    renderRow(makeSession({ driver: 'claude' }))

    const badge = screen.getByText('Claude')
    expect(badge.className).not.toContain('text-(--ui-text-tertiary)')
  })
})
