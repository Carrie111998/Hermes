import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $compactingSessions, setSessionCompacting } from '@/store/compaction'

import { CompactionGuard } from './compaction-guard'

const SID = 'runtime-1'

describe('CompactionGuard', () => {
  beforeEach(() => $compactingSessions.set({}))

  afterEach(() => {
    cleanup()
    $compactingSessions.set({})
  })

  it('opens only for the session currently being compacted', () => {
    render(<CompactionGuard sessionId={SID} />)

    act(() => setSessionCompacting('runtime-2', true))
    expect(screen.queryByRole('dialog')).toBeNull()

    act(() => setSessionCompacting(SID, true))
    expect(screen.getByRole('dialog')).not.toBeNull()
    expect(screen.getByRole('heading', { name: 'Compressing this session' })).not.toBeNull()
    expect(screen.getByText(/Sending messages and changing this session are temporarily disabled/i)).not.toBeNull()
  })

  it('cannot be dismissed while compaction remains active', async () => {
    setSessionCompacting(SID, true)
    render(<CompactionGuard sessionId={SID} />)
    const downstreamShortcut = vi.fn()

    window.addEventListener('keydown', downstreamShortcut)

    expect(screen.queryByRole('button', { name: /close/i })).toBeNull()
    fireEvent.keyDown(window.document, { key: 'Escape' })
    fireEvent.keyDown(window, { ctrlKey: true, key: 'n' })

    // Radix installs its outside-pointer listener on a deferred tick.
    await new Promise(resolve => setTimeout(resolve, 10))
    const overlay = window.document.querySelector('[data-slot="dialog-overlay"]') as HTMLElement
    fireEvent.pointerDown(overlay, { button: 0 })
    fireEvent.pointerUp(overlay, { button: 0 })
    fireEvent.click(overlay, { button: 0 })
    await new Promise(resolve => setTimeout(resolve, 10))

    expect(screen.getByRole('dialog')).not.toBeNull()
    expect(downstreamShortcut).not.toHaveBeenCalled()

    window.removeEventListener('keydown', downstreamShortcut)
  })

  it('unlocks only when the structured compaction state clears', () => {
    setSessionCompacting(SID, true)
    render(<CompactionGuard sessionId={SID} />)

    act(() => setSessionCompacting(SID, false))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
