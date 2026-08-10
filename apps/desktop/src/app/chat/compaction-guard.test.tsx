import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import { $compactingSessions, setSessionCompacting } from '@/store/compaction'

import { CompactionGuard } from './compaction-guard'

const SID = 'runtime-1'
const LABEL = 'Context compaction guard work'

function renderGuard(extra?: ReactNode) {
  return render(
    <>
      <div className="relative" data-testid="chat-surface">
        <CompactionGuard sessionId={SID} sessionLabel={LABEL} />
      </div>
      {extra}
    </>
  )
}

describe('CompactionGuard', () => {
  beforeEach(() => $compactingSessions.set({}))

  afterEach(() => {
    cleanup()
    $compactingSessions.set({})
  })

  it('opens only for the affected session and names it precisely', () => {
    renderGuard()

    act(() => setSessionCompacting('runtime-2', true))
    expect(screen.queryByRole('dialog')).toBeNull()

    act(() => setSessionCompacting(SID, true))
    expect(screen.getByRole('heading', { name: `Compressing “${LABEL}”` })).not.toBeNull()
    expect(screen.getByText('Session runtime-')).not.toBeNull()
    expect(screen.getByText(/Only this chat is temporarily locked/i)).not.toBeNull()
  })

  it('blocks only its chat pane while other Desktop controls remain usable', () => {
    setSessionCompacting(SID, true)
    const onOtherSession = vi.fn()
    const downstreamShortcut = vi.fn()

    renderGuard(<button onClick={onOtherSession}>Open another session</button>)
    window.addEventListener('keydown', downstreamShortcut)

    const dialog = screen.getByRole('dialog')
    const chatSurface = screen.getByTestId('chat-surface')
    const otherSession = screen.getByRole('button', { name: 'Open another session' })

    expect(chatSurface.contains(dialog)).toBe(true)
    expect(window.document.querySelector('[data-slot="dialog-overlay"]')).toBeNull()
    expect(screen.queryByRole('button', { name: /close/i })).toBeNull()

    fireEvent.keyDown(dialog, { ctrlKey: true, key: 'n' })
    expect(downstreamShortcut).not.toHaveBeenCalled()

    fireEvent.click(otherSession)
    fireEvent.keyDown(otherSession, { ctrlKey: true, key: 'n' })
    expect(onOtherSession).toHaveBeenCalledTimes(1)
    expect(downstreamShortcut).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('dialog')).not.toBeNull()

    window.removeEventListener('keydown', downstreamShortcut)
  })

  it('does not steal focus when a hidden/background pane starts compacting', () => {
    render(
      <>
        <button>Visible session control</button>
        <PaneVisibleContext.Provider value={false}>
          <div className="relative">
            <CompactionGuard sessionId={SID} sessionLabel={LABEL} />
          </div>
        </PaneVisibleContext.Provider>
      </>
    )
    const visibleControl = screen.getByRole('button', { name: 'Visible session control' })
    visibleControl.focus()

    act(() => setSessionCompacting(SID, true))

    expect(window.document.activeElement).toBe(visibleControl)
  })

  it('unlocks only when the structured compaction state clears', () => {
    setSessionCompacting(SID, true)
    renderGuard()

    act(() => setSessionCompacting(SID, false))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
