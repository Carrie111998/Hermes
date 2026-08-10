import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

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

  it('cannot be dismissed while compaction remains active', () => {
    setSessionCompacting(SID, true)
    render(<CompactionGuard sessionId={SID} />)

    expect(screen.queryByRole('button', { name: /close/i })).toBeNull()
    fireEvent.keyDown(window.document, { key: 'Escape' })
    expect(screen.getByRole('dialog')).not.toBeNull()
  })

  it('unlocks only when the structured compaction state clears', () => {
    setSessionCompacting(SID, true)
    render(<CompactionGuard sessionId={SID} />)

    act(() => setSessionCompacting(SID, false))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
