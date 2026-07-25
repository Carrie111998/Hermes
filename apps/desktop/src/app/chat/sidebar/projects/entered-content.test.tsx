import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import { $sidebarWorkspaceNodeOpen } from '@/store/layout'

import { EnteredProjectContent } from './entered-content'
import type { SidebarProjectTree, SidebarSessionGroup, SidebarWorkspaceTree } from './workspace-groups'

afterEach(cleanup)

beforeEach(() => {
  // Collapse state is persisted per node; reset it so one test's collapse can't
  // leak into the next.
  $sidebarWorkspaceNodeOpen.set({})
})

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel' },
      sidebar: {
        newSessionIn: (label: string) => `New session in ${label}`,
        noSessions: 'No sessions yet',
        projects: {
          forceRemove: 'Force remove',
          removeFromSidebar: 'Hide from sidebar',
          removeWorktree: 'Remove worktree',
          removeWorktreeConfirm: 'confirm',
          removeWorktreeDirty: 'dirty',
          removeWorktreeFailed: 'failed'
        },
        showMoreIn: (n: number, label: string) => `Show ${n} more in ${label}`
      },
      statusStack: { coding: { switchFailed: (branch: string) => `switch ${branch}` } }
    }
  })
}))

let nextId = 0

const session = (overrides: Partial<SessionInfo> = {}): SessionInfo =>
  ({
    archived: false,
    cwd: '/repo',
    id: `s${nextId++}`,
    is_active: false,
    last_active: 1_000,
    message_count: 1,
    started_at: 1_000,
    title: null,
    ...overrides
  }) as unknown as SessionInfo

const lane = (over: Partial<SidebarSessionGroup> & Pick<SidebarSessionGroup, 'id' | 'label'>): SidebarSessionGroup => ({
  path: '/repo',
  sessions: [],
  ...over
})

const project = (
  groups: SidebarSessionGroup[],
  repoOverrides: Partial<SidebarWorkspaceTree> = {}
): SidebarProjectTree => {
  const sessionCount = groups.reduce((sum, group) => sum + group.sessions.length, 0)

  return {
    id: 'p1',
    label: 'hermes-agent',
    path: '/repo',
    repos: [{ groups, id: '/repo', label: 'repo', path: '/repo', sessionCount, ...repoOverrides }],
    sessionCount
  } as unknown as SidebarProjectTree
}

// Rows are rendered by the caller (the real sidebar renders SessionRow); a plain
// title list is enough to assert what the user can see without expanding.
const renderRows = (sessions: SessionInfo[]) => (
  <div>
    {sessions.map(s => (
      <div key={s.id}>{s.title}</div>
    ))}
  </div>
)

// The lane header splits its label across two spans (head + tail, for truncation),
// so the label is only queryable via the header's title attribute.
const laneHeaderLabels = (container: HTMLElement): string[] =>
  [...container.querySelectorAll('span[title]')].map(el => (el.getAttribute('title') ?? '').split('\n')[0])

describe('EnteredProjectContent', () => {
  it('renders main-checkout sessions with no branch header when it is the repo\u2019s only lane', () => {
    const { container } = render(
      <EnteredProjectContent
        project={project([
          lane({
            id: '/repo::branch::main',
            label: 'main',
            isHome: true,
            isMain: true,
            sessions: [session({ title: 'ship the fix' }), session({ title: 'read the docs' })]
          })
        ])}
        renderRows={renderRows}
      />
    )

    expect(laneHeaderLabels(container)).toEqual([])
    expect(screen.getByText('ship the fix')).toBeTruthy()
    expect(screen.getByText('read the docs')).toBeTruthy()
  })

  it('shows those sessions even when the lane was collapsed before the header went away', () => {
    // The reported symptom: a persisted collapse left a body of nothing but
    // `main (N)`. A headerless lane has no toggle, so it must ignore that state.
    $sidebarWorkspaceNodeOpen.set({ '/repo::branch::main': false })

    render(
      <EnteredProjectContent
        project={project([
          lane({
            id: '/repo::branch::main',
            label: 'main',
            isHome: true,
            isMain: true,
            sessions: [session({ title: 'ship the fix' })]
          })
        ])}
        renderRows={renderRows}
      />
    )

    expect(screen.getByText('ship the fix')).toBeTruthy()
  })

  it('keeps every lane header once a linked worktree exists', () => {
    const { container } = render(
      <EnteredProjectContent
        project={project([
          lane({
            id: '/repo::branch::main',
            label: 'main',
            isHome: true,
            isMain: true,
            sessions: [session({ title: 'on trunk' })]
          }),
          lane({
            id: '/repo-wt-feature',
            label: 'feature',
            path: '/repo-wt-feature',
            sessions: [session({ cwd: '/repo-wt-feature', title: 'on feature' })]
          })
        ])}
        renderRows={renderRows}
      />
    )

    expect(laneHeaderLabels(container)).toEqual(['main', 'feature'])
    expect(screen.getByText('on trunk')).toBeTruthy()
    expect(screen.getByText('on feature')).toBeTruthy()
  })

  it('keeps per-repo headers for a multi-folder project', () => {
    const multi = {
      id: 'p1',
      label: 'monorepo',
      path: '/repo',
      sessionCount: 2,
      repos: [
        {
          groups: [lane({ id: '/repo::branch::main', label: 'main', isMain: true, sessions: [session()] })],
          id: '/repo',
          label: 'repo',
          path: '/repo',
          sessionCount: 1
        },
        {
          groups: [
            lane({
              id: '/other::branch::main',
              label: 'main',
              isMain: true,
              path: '/other',
              sessions: [session({ cwd: '/other' })]
            })
          ],
          id: '/other',
          label: 'other',
          path: '/other',
          sessionCount: 1
        }
      ]
    } as unknown as SidebarProjectTree

    const { container } = render(<EnteredProjectContent project={multi} renderRows={renderRows} />)

    // Both repo headers stay; their lone main lanes still flatten inside them.
    expect(laneHeaderLabels(container)).toEqual(['repo', 'other'])
  })
})
