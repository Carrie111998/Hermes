import { describe, expect, it } from 'vitest'

import { resolveFocusedWorkspaceCwd } from './use-focused-workspace-cwd'

const base = {
  focusedRowCwd: '',
  focusedStateCwd: '',
  focusedStateStoredId: null,
  focusedStoredSessionId: null,
  liveCwdSharesFocusLineage: false,
  primaryCwd: '/primary',
  selectedStoredSessionId: 'primary-session',
  workspaceCwdOwner: 'primary-session'
}

describe('resolveFocusedWorkspaceCwd', () => {
  it('uses the primary CWD only while its ownership token matches the selected session', () => {
    expect(resolveFocusedWorkspaceCwd(base)).toBe('/primary')
    expect(resolveFocusedWorkspaceCwd({ ...base, workspaceCwdOwner: 'previous-session' })).toBe('')
  })

  it('keeps the live primary CWD ahead of its launch row after an in-session relocation', () => {
    expect(resolveFocusedWorkspaceCwd({ ...base, focusedRowCwd: '/primary-at-launch' })).toBe('/primary')
  })

  it('uses a focused tile row instead of inheriting the primary workspace', () => {
    expect(
      resolveFocusedWorkspaceCwd({
        ...base,
        focusedRowCwd: '/workspaces/beta-project',
        focusedStoredSessionId: 'beta-session'
      })
    ).toBe('/workspaces/beta-project')

    expect(resolveFocusedWorkspaceCwd({ ...base, focusedStoredSessionId: 'detached-session' })).toBe('')
  })

  it('ignores a lagging runtime CWD that belongs to the previously focused session', () => {
    expect(
      resolveFocusedWorkspaceCwd({
        ...base,
        focusedRowCwd: '/workspaces/beta-project',
        focusedStateCwd: '/workspaces/alpha-project',
        focusedStateStoredId: 'alpha-session',
        focusedStoredSessionId: 'beta-session'
      })
    ).toBe('/workspaces/beta-project')
  })

  it('accepts a live CWD from the focused session lineage', () => {
    expect(
      resolveFocusedWorkspaceCwd({
        ...base,
        focusedRowCwd: '/workspaces/beta-project',
        focusedStateCwd: '/workspaces/beta-project/.worktrees/fix',
        focusedStateStoredId: 'beta-child-session',
        focusedStoredSessionId: 'beta-root-session',
        liveCwdSharesFocusLineage: true
      })
    ).toBe('/workspaces/beta-project/.worktrees/fix')
  })
})
