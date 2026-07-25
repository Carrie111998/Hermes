import { act, cleanup, render } from '@testing-library/react'
import { type RefObject, useRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PANE_HIDDEN_ATTR } from '@/components/pane-shell/pane-visibility'

import { useComposerMetrics } from './use-composer-metrics'

vi.mock('@assistant-ui/react', () => ({
  useAuiState: (selector: (state: { composer: { text: string } }) => unknown) => selector({ composer: { text: '' } })
}))

vi.mock('@/store/composer-popout', () => ({
  $composerPoppedOut: { get: () => false }
}))

vi.mock('@/store/windows', () => ({
  isSecondaryWindow: () => false
}))

const observers = new Set<TestResizeObserver>()

class TestResizeObserver {
  private readonly elements = new Set<Element>()

  constructor(private readonly callback: ResizeObserverCallback) {
    observers.add(this)
  }

  observe(element: Element) {
    this.elements.add(element)
  }

  disconnect() {
    observers.delete(this)
    this.elements.clear()
  }

  trigger() {
    this.callback(
      [...this.elements].map(target => ({ target }) as ResizeObserverEntry),
      this as unknown as ResizeObserver
    )
  }
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)

function setRect(element: HTMLElement, width: number, height: number) {
  element.getBoundingClientRect = () =>
    ({
      bottom: height,
      height,
      left: 0,
      right: width,
      top: 0,
      width,
      x: 0,
      y: 0,
      toJSON: () => ({})
    }) as DOMRect
}

function triggerObservers() {
  act(() => {
    for (const observer of observers) {
      observer.trigger()
    }
  })
}

function rootStyle(name: string) {
  return globalThis.document.documentElement.style.getPropertyValue(name)
}

function MetricsPane({ hidden, height, surfaceHeight, width = 520 }: { hidden?: boolean; height: number; surfaceHeight: number; width?: number }) {
  const composerRef = useRef<HTMLFormElement | null>(null)
  const surfaceRef = useRef<HTMLDivElement | null>(null)
  const editorRef = useRef<HTMLDivElement | null>(null)

  useComposerMetrics({
    composerRef: composerRef as RefObject<HTMLFormElement | null>,
    composerSurfaceRef: surfaceRef as RefObject<HTMLDivElement | null>,
    editorRef: editorRef as RefObject<HTMLDivElement | null>,
    poppedOut: false
  })

  return (
    <div {...(hidden ? { [PANE_HIDDEN_ATTR]: '' } : {})}>
      <form
        ref={node => {
          composerRef.current = node

          if (node) {
            setRect(node, width, height)
          }
        }}
      >
        <div
          ref={node => {
            surfaceRef.current = node

            if (node) {
              setRect(node, width, surfaceHeight)
            }
          }}
        />
        <div ref={editorRef} />
      </form>
    </div>
  )
}

afterEach(() => {
  cleanup()
  observers.clear()
  globalThis.document.documentElement.style.removeProperty('--composer-measured-height')
  globalThis.document.documentElement.style.removeProperty('--composer-surface-measured-height')
})

describe('useComposerMetrics pane visibility', () => {
  it('publishes and updates root metrics from the visible composer', () => {
    const { rerender } = render(<MetricsPane height={80} surfaceHeight={56} />)
    triggerObservers()

    expect(rootStyle('--composer-measured-height')).toBe('80px')
    expect(rootStyle('--composer-surface-measured-height')).toBe('56px')

    rerender(<MetricsPane height={104} surfaceHeight={72} />)
    triggerObservers()

    expect(rootStyle('--composer-measured-height')).toBe('104px')
    expect(rootStyle('--composer-surface-measured-height')).toBe('72px')
  })

  it('keeps foreground root metrics when a hidden kept-alive pane measures later', () => {
    render(
      <>
        <MetricsPane height={88} surfaceHeight={64} />
        <MetricsPane height={32} hidden surfaceHeight={24} />
      </>
    )

    triggerObservers()

    expect(rootStyle('--composer-measured-height')).toBe('88px')
    expect(rootStyle('--composer-surface-measured-height')).toBe('64px')
  })

  it('keeps foreground root metrics when a hidden kept-alive pane unmounts', () => {
    const { rerender } = render(
      <>
        <MetricsPane height={88} surfaceHeight={64} />
        <MetricsPane height={32} hidden surfaceHeight={24} />
      </>
    )

    triggerObservers()

    rerender(<MetricsPane height={88} surfaceHeight={64} />)

    expect(rootStyle('--composer-measured-height')).toBe('88px')
    expect(rootStyle('--composer-surface-measured-height')).toBe('64px')
  })
})
