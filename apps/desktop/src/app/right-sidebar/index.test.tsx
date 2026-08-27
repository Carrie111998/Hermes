import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'
import { $layoutTree, activateTreePane, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import type { HermesReadDirResult } from '@/global'
import { $connection, $selectedStoredSessionId, $workspaceCwdOwner, setCurrentCwd, setSessions } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'
import { makeSessionInfo } from '@/test/session-info'

import { resetProjectTreeState } from './files/use-project-tree'

import { RightSidebarPane } from './index'

const readDir = vi.fn<(path: string) => Promise<HermesReadDirResult>>()

function installBridge() {
  ;(window as unknown as { hermesDesktop: { readDir: typeof readDir } }).hermesDesktop = { readDir }
}

describe('RightSidebarPane', () => {
  beforeEach(() => {
    $connection.set(null)
    $selectedStoredSessionId.set(null)
    $workspaceCwdOwner.set(null)
    $layoutTree.set(null)
    $sessionTiles.set([])
    setSessions([])
    noteActiveTreeGroup(null)
    resetProjectTreeState()
    readDir.mockReset()
    readDir.mockResolvedValue({ entries: [{ isDirectory: false, name: 'README.md', path: '/repo/README.md' }] })
    installBridge()
  })

  afterEach(() => {
    cleanup()
    $connection.set(null)
    $selectedStoredSessionId.set(null)
    $workspaceCwdOwner.set(null)
    $layoutTree.set(null)
    $sessionTiles.set([])
    setSessions([])
    noteActiveTreeGroup(null)
    setCurrentCwd('')
    resetProjectTreeState()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('renders the tree whenever the session has a working dir (repo or not) — no picker', async () => {
    setCurrentCwd('/repo')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    const refresh = await screen.findByRole('button', { name: 'Refresh tree' })

    readDir.mockClear()
    fireEvent.click(refresh)
    await waitFor(() => expect(readDir).toHaveBeenCalledWith('/repo'))

    // The freeform folder picker is retired.
    expect(screen.queryByRole('button', { name: 'Open folder' })).toBeNull()
  })

  it('does not read a retained cwd while it belongs to a previous session', async () => {
    $selectedStoredSessionId.set('new-session')
    $workspaceCwdOwner.set('previous-session')
    setCurrentCwd('/home/doug/default-profile-workspace')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Refresh tree' })).toBeNull())
    expect(readDir).not.toHaveBeenCalled()
  })

  it('shows no tree for a detached chat (no working dir)', async () => {
    setCurrentCwd('')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Refresh tree' })).toBeNull())
    expect(readDir).not.toHaveBeenCalled()
  })

  it('re-roots the Files tree when a top session tab is focused', async () => {
    const alphaCwd = '/Users/test/Projects/alpha-workspace'
    const betaCwd = '/Users/test/Projects/beta-project'

    setSessions([
      makeSessionInfo({ cwd: alphaCwd, id: 'alpha-session' }),
      makeSessionInfo({ cwd: betaCwd, id: 'beta-session' })
    ])
    $selectedStoredSessionId.set('alpha-session')
    $workspaceCwdOwner.set('alpha-session')
    setCurrentCwd(alphaCwd)
    $sessionTiles.set([{ storedSessionId: 'beta-session' }])
    $layoutTree.set(
      split('row', [
        group(['workspace', 'session-tile:beta-session'], {
          active: 'workspace',
          id: 'main'
        }),
        group(['files'], { active: 'files', id: 'files-zone' })
      ])
    )
    noteActiveTreeGroup('main')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    await waitFor(() => expect(readDir).toHaveBeenCalledWith(alphaCwd))
    expect(screen.getByText('alpha-workspace')).toBeTruthy()

    act(() => activateTreePane('main', 'session-tile:beta-session'))

    await waitFor(() => expect(readDir).toHaveBeenCalledWith(betaCwd))
    expect(screen.getByText('beta-project')).toBeTruthy()
    expect(screen.queryByText('alpha-workspace')).toBeNull()

    // Using the Files rail changes keyboard focus to its zone, but must not
    // detach the rail from the active top session tab.
    act(() => noteActiveTreeGroup('files-zone'))
    await waitFor(() => expect(screen.getByText('beta-project')).toBeTruthy())
    expect(readDir.mock.calls.at(-1)?.[0]).toBe(betaCwd)

    act(() => activateTreePane('main', 'workspace'))

    await waitFor(() => expect(screen.getByText('alpha-workspace')).toBeTruthy())
    expect(screen.queryByText('beta-project')).toBeNull()
  })
})
