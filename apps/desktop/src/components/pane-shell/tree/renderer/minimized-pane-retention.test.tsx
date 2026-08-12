import { useEffect, useState } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { registry } from '@/contrib/registry'

import { group } from '../model'
import { $dismissedPanes, $hiddenTreePanes, $layoutTree, declareDefaultTree, setTreeGroupMinimized } from '../store'

import { TreeGroup } from './tree-group'

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
  vi.stubGlobal('CSS', { ...globalThis.CSS, escape: (value: string) => value })
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

describe('minimized TreeGroup content retention', () => {
  const disposers: (() => void)[] = []
  let mounts = 0
  let unmounts = 0
  let controllerSequence = 0

  beforeEach(() => {
    vi.useFakeTimers()
    window.localStorage.clear()
    $dismissedPanes.set(new Set())
    $hiddenTreePanes.set(new Set())
    controllerSequence = 0
    mounts = 0
    unmounts = 0
    $layoutTree.set(null)
  })

  afterEach(() => {
    cleanup()
    disposers.splice(0).forEach(dispose => dispose())
    vi.useRealTimers()
  })

  it('keeps a pending drawing controller mounted and hidden through minimize and restore', () => {
    function DrawingControllerHarness() {
      const visible = usePaneVisible()
      const [controllerId] = useState(() => ++controllerSequence)
      const [scene, setScene] = useState<'saved' | 'pending'>('saved')

      useEffect(() => {
        mounts += 1

        return () => {
          unmounts += 1
        }
      }, [])

      useEffect(() => {
        if (scene !== 'pending') {
          return
        }

        const timer = window.setTimeout(() => setScene('saved'), 500)

        return () => window.clearTimeout(timer)
      }, [scene, setScene])

      return (
        <button data-controller={controllerId} data-testid="drawing" data-visible={String(visible)} onClick={() => setScene('pending')}>
          {scene}
        </button>
      )
    }

    disposers.push(
      registry.register({ area: 'panes', data: { placement: 'right' }, id: 'drawing', render: () => <DrawingControllerHarness />, title: 'drawing' })
    )
    declareDefaultTree(group(['drawing'], { id: 'drawing-zone' }))

    const node = () => {
      const tree = $layoutTree.get()

      if (!tree || tree.type !== 'group') {
        throw new Error('expected drawing group')
      }

      return tree
    }
    const rendered = render(<TreeGroup node={node()} parentAxis="row" railSide="right" />)
    fireEvent.click(screen.getByTestId('drawing'))
    expect(screen.getByTestId('drawing').textContent).toBe('pending')

    setTreeGroupMinimized('drawing-zone', true)
    rendered.rerender(<TreeGroup node={node()} parentAxis="row" railSide="right" />)

    const minimizedDrawing = screen.getByTestId('drawing')
    expect(minimizedDrawing.textContent).toBe('pending')
    expect(minimizedDrawing.dataset.visible).toBe('false')
    expect(minimizedDrawing.closest('[aria-hidden="true"]')).toBeTruthy()
    expect(minimizedDrawing.closest('[data-pane-hidden]')).toBeTruthy()
    expect(mounts).toBe(1)
    expect(unmounts).toBe(0)

    act(() => vi.advanceTimersByTime(500))
    expect(screen.getByTestId('drawing').textContent).toBe('saved')

    setTreeGroupMinimized('drawing-zone', false)
    rendered.rerender(<TreeGroup node={node()} parentAxis="row" railSide="right" />)

    expect(screen.getByTestId('drawing').dataset.controller).toBe('1')
    expect(screen.getByTestId('drawing').dataset.visible).toBe('true')
    expect(mounts).toBe(1)
    expect(unmounts).toBe(0)
  })
})

