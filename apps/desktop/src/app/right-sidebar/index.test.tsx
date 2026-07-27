import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesReadDirResult } from '@/global'
import { $connection, $currentCwd, setActiveSessionId, setCurrentCwd } from '@/store/session'

import { resetProjectTreeState } from './files/use-project-tree'

import { RightSidebarPane } from './index'

const readDir = vi.fn<(path: string) => Promise<HermesReadDirResult>>()

function installBridge() {
  ;(window as unknown as { hermesDesktop: { readDir: typeof readDir } }).hermesDesktop = { readDir }
}

describe('RightSidebarPane', () => {
  beforeEach(() => {
    $connection.set(null)
    setActiveSessionId(null)
    resetProjectTreeState()
    readDir.mockReset()
    readDir.mockResolvedValue({ entries: [{ isDirectory: false, name: 'README.md', path: '/repo/README.md' }] })
    installBridge()
  })

  afterEach(() => {
    cleanup()
    $connection.set(null)
    setActiveSessionId(null)
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

  it('browses the parent directory without changing the session workspace', async () => {
    setCurrentCwd('/repo/child')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    const up = await screen.findByRole('button', { name: 'Go to parent folder' })

    readDir.mockClear()
    fireEvent.click(up)

    await waitFor(() => expect(readDir).toHaveBeenCalledWith('/repo'))
    expect($currentCwd.get()).toBe('/repo/child')
  })

  it('browses to the POSIX root from a top-level directory', async () => {
    setCurrentCwd('/repo')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    const up = await screen.findByRole('button', { name: 'Go to parent folder' })

    readDir.mockClear()
    fireEvent.click(up)

    await waitFor(() => expect(readDir).toHaveBeenCalledWith('/'))
  })

  it('resets the browser when switching sessions with the same workspace', async () => {
    setCurrentCwd('/repo/child')
    setActiveSessionId('session-a')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Go to parent folder' }))
    await waitFor(() => expect(readDir).toHaveBeenCalledWith('/repo'))

    readDir.mockClear()
    act(() => setActiveSessionId('session-b'))

    await waitFor(() => expect(readDir).toHaveBeenCalledWith('/repo/child'))
  })

  it('browses to the root of a Windows drive', async () => {
    setCurrentCwd('C:\\repo')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    const up = await screen.findByRole('button', { name: 'Go to parent folder' })

    readDir.mockClear()
    fireEvent.click(up)

    await waitFor(() => expect(readDir).toHaveBeenCalledWith('C:\\'))
  })

  it('stops at a Windows UNC share root', async () => {
    setCurrentCwd('\\\\server\\share\\folder')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    const up = await screen.findByRole('button', { name: 'Go to parent folder' })

    readDir.mockClear()
    fireEvent.click(up)

    await waitFor(() => expect(readDir).toHaveBeenCalledWith('\\\\server\\share'))
    await waitFor(() => expect((up as HTMLButtonElement).disabled).toBe(true))
  })

  it('shows no tree for a detached chat (no working dir)', async () => {
    setCurrentCwd('')

    render(<RightSidebarPane onActivateFile={vi.fn()} onActivateFolder={vi.fn()} />)

    await waitFor(() => expect(screen.queryByRole('button', { name: 'Refresh tree' })).toBeNull())
    expect(readDir).not.toHaveBeenCalled()
  })
})
