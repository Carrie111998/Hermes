import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { markActiveComposer } from '../focus'
import { ComposerSurfaceProvider } from '../scope'

import { useComposerEscCancel } from './use-composer-esc-cancel'

function Harness({ busy, onCancel }: { busy: boolean; onCancel: () => void }) {
  useComposerEscCancel({ awaitingInput: false, busy, onCancel, target: 'main' })

  return null
}

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
  markActiveComposer('main')
})

describe('useComposerEscCancel', () => {
  it('cancels the turn on Esc while busy with chat focus', () => {
    const onCancel = vi.fn()
    render(<Harness busy onCancel={onCancel} />)

    fireEvent.keyDown(window, { key: 'Escape' })

    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('cancels only the active surface when two busy composers share a target', () => {
    const firstCancel = vi.fn()
    const secondCancel = vi.fn()

    render(
      <>
        <ComposerSurfaceProvider value="surface-a">
          <Harness busy onCancel={firstCancel} />
        </ComposerSurfaceProvider>
        <ComposerSurfaceProvider value="surface-b">
          <Harness busy onCancel={secondCancel} />
        </ComposerSurfaceProvider>
      </>
    )
    markActiveComposer('main', 'surface-b')

    fireEvent.keyDown(window, { key: 'Escape' })

    expect(firstCancel).not.toHaveBeenCalled()
    expect(secondCancel).toHaveBeenCalledOnce()
  })

  it('does not cancel on Esc when the turn is not busy', () => {
    const onCancel = vi.fn()
    render(<Harness busy={false} onCancel={onCancel} />)

    fireEvent.keyDown(window, { key: 'Escape' })

    expect(onCancel).not.toHaveBeenCalled()
  })

  it('does not cancel on Esc while an overlay surface covers the chat', () => {
    const onCancel = vi.fn()
    render(<Harness busy onCancel={onCancel} />)

    // OverlayView stamps `data-overlay-surface` on its root — the same signal
    // the type-to-focus path uses to stand down while an overlay is open.
    const overlay = document.createElement('div')
    overlay.setAttribute('data-overlay-surface', '')
    document.body.appendChild(overlay)

    fireEvent.keyDown(window, { key: 'Escape' })

    expect(onCancel).not.toHaveBeenCalled()
  })
})
