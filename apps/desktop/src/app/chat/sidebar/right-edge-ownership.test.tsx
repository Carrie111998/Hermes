/**
 * Regression test for PR #90920 — "Sidebar rows own their right edge".
 *
 * Renders the REAL sidebar chrome components and asserts the geometry
 * contract at the DOM level: the SHELL owns the trailing (right-edge) inset.
 *
 * Post-fix contract:
 *   - SidebarRowShell:      owns the trailing inset (rowPadTrail = 'pr-2')
 *   - actions slot:         self-stretch (drag exclusion covers the whole
 *                           column), inset by the shell's own pr-2
 *   - SidebarRowBody:       rowPadX = 'pl-2 pr-2' (lead inset + the
 *                           label-to-actions gap, both inside the click target)
 *   - load-more ellipsis:   mr-2 (repeats the row's inset — it hangs off the
 *                           list where no row geometry reaches it)
 *   - workspace "show more": same mr-2 repeat
 *
 * Every trailing thing a row renders — age, chips, kebab, cron countdown,
 * project totals, ellipses — therefore stops on one right edge (the shell's
 * pr-2) instead of ending flush on the border box where .arc-row paints its
 * travelling ring (--arc-standoff: 0rem, styles.css).
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SidebarRowBody, SidebarRowShell } from './chrome'
import { SidebarLoadMoreRow } from './load-more-row'
import { WorkspaceShowMoreButton } from './projects/workspace-header'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        loadCount: (n: number) => `Load ${n} more`,
        loadMore: 'Load more',
        loading: 'Loading…',
        projects: {
          copyPath: 'Copy path',
          menu: 'Actions',
          removeWorktree: 'Remove worktree',
          reveal: 'Reveal in file manager',
          startWork: 'New worktree'
        },
        showMoreIn: (n: number, label: string) => `Show ${n} more in ${label}`
      }
    }
  })
}))

vi.mock('@/store/projects', () => ({
  copyPath: vi.fn(),
  revealPath: vi.fn()
}))

vi.mock('@/store/coding-status', () => ({
  openWorktreeDialog: vi.fn()
}))

const TRAILING = /(?:^|\s)(?:pr|mr)-[\d.]+(?:\s|$)/

describe('right-edge ownership (PR #90920)', () => {
  it('SidebarRowShell owns the trailing inset: shell pr-2, actions self-stretch, body pl-2 pr-2', () => {
    const { container } = render(
      <SidebarRowShell actions={<span>age</span>}>
        <SidebarRowBody>title</SidebarRowBody>
      </SidebarRowShell>
    )

    // The shell is the outer grid and now owns the trailing inset — one class
    // (rowPadTrail = 'pr-2') insets every trailing thing a row can render.
    const shell = container.firstElementChild as HTMLElement
    expect(shell.className).toContain('grid grid-cols-[minmax(0,1fr)_auto]')
    expect(shell.className).toMatch(/pr-2/)

    // The actions slot stretches so the drag-exclusion selector covers the
    // whole column, not just its tallest control — and it needs no padding of
    // its own, because the shell's pr-2 already insets it.
    const actions = shell.querySelector('[data-row-actions]') as HTMLElement
    expect(actions).toBeTruthy()
    expect(actions.className).toMatch(/self-stretch/)
    expect(actions.className).not.toMatch(/self-center/)
    expect(actions.className).not.toMatch(TRAILING)

    // The body carries rowPadX = 'pl-2 pr-2': the lead's inset plus the
    // label-to-actions gap, both inside the row's click target.
    const body = shell.querySelector('button') as HTMLElement
    expect(body.className).toMatch(/pl-2/)
    expect(body.className).toMatch(/pr-2/)
  })

  it('the one-line session row actions column is inset by the shell, not flush', () => {
    // session-row.tsx passes its actions cluster to the shell's actions slot,
    // so the trailing text now stops at the shell's pr-2 instead of the
    // border box the arc paints on.
    const { container } = render(
      <SidebarRowShell actions={<span data-testid="trailing">5m</span>}>
        <SidebarRowBody>Session title</SidebarRowBody>
      </SidebarRowShell>
    )

    const trailing = container.querySelector('[data-row-actions]') as HTMLElement
    expect(trailing).toBeTruthy()
    // No per-caller padding: the inset is owned by the shell one level up.
    expect(trailing.className).not.toMatch(TRAILING)

    const shell = container.firstElementChild as HTMLElement
    expect(shell.className).toMatch(/pr-2/)
  })

  it('the load-more ellipsis repeats the row trailing inset with mr-2', () => {
    render(<SidebarLoadMoreRow onClick={() => {}} step={20} />)

    const button = screen.getByRole('button')
    expect(button.className).toMatch(/ml-auto/)
    expect(button.className).toMatch(/mr-2/)
  })

  it('the workspace "show more" ellipsis repeats the row trailing inset with mr-2', () => {
    render(<WorkspaceShowMoreButton count={5} label="Test D" onClick={() => {}} />)

    const button = screen.getByRole('button', { name: 'Show 5 more in Test D' })
    expect(button.className).toMatch(/ml-auto/)
    expect(button.className).toMatch(/mr-2/)
  })
})
