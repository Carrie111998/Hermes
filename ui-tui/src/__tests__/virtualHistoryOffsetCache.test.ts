import { PassThrough } from 'stream'

import { Box, renderSync, ScrollBox, type ScrollBoxHandle, Text } from '@hermes/ink'
import React, { useLayoutEffect, useRef } from 'react'
import { describe, expect, it } from 'vitest'

import { useVirtualHistory, virtualHistorySnapshotKey } from '../hooks/useVirtualHistory.js'

interface Item {
  height: number
  heightAfterResize?: number
  key: string
}

interface Exposed {
  scroll: ScrollBoxHandle | null
  virtualHistory: ReturnType<typeof useVirtualHistory>
}

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const makeStreams = () => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()

  Object.assign(stdout, { columns: 80, isTTY: false, rows: 20 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', () => {})

  return { stderr, stdin, stdout }
}

const mountedSpan = (items: readonly Item[], virtualHistory: ReturnType<typeof useVirtualHistory>) => {
  let height = 0

  for (let index = virtualHistory.start; index < virtualHistory.end; index++) {
    height += items[index]?.height ?? 0
  }

  return { bottom: virtualHistory.topSpacer + height, top: virtualHistory.topSpacer }
}

const viewportIsMounted = (
  items: readonly Item[],
  virtualHistory: ReturnType<typeof useVirtualHistory>,
  scroll: ScrollBoxHandle
) => {
  const span = mountedSpan(items, virtualHistory)
  const top = scroll.getScrollTop()
  const bottom = top + scroll.getViewportHeight()

  return top >= span.top && bottom <= span.bottom
}

const itemHeightForColumns = (item: Item | undefined, columns: number) =>
  columns >= 80 ? (item?.heightAfterResize ?? item?.height ?? 1) : (item?.height ?? 1)

function Harness({
  columns = 80,
  expose,
  height = 10,
  items,
  maxMounted = 16
}: {
  columns?: number
  expose: React.MutableRefObject<Exposed | null>
  height?: number
  items: readonly Item[]
  maxMounted?: number
}) {
  const scrollRef = useRef<ScrollBoxHandle | null>(null)

  const virtualHistory = useVirtualHistory(scrollRef, items, columns, {
    coldStartCount: 16,
    estimateHeight: index => itemHeightForColumns(items[index], columns),
    maxMounted,
    overscan: 2
  })

  useLayoutEffect(() => {
    expose.current = { scroll: scrollRef.current, virtualHistory }
  })

  return React.createElement(
    ScrollBox,
    { flexDirection: 'column', height, ref: scrollRef, stickyScroll: true },
    React.createElement(
      Box,
      { flexDirection: 'column', width: '100%' },
      virtualHistory.topSpacer > 0 ? React.createElement(Box, { height: virtualHistory.topSpacer }) : null,
      ...items.slice(virtualHistory.start, virtualHistory.end).map(item =>
        React.createElement(
          Box,
          {
            height: itemHeightForColumns(item, columns),
            key: item.key,
            ref: virtualHistory.measureRef(item.key)
          },
          React.createElement(Text, null, item.key)
        )
      ),
      virtualHistory.bottomSpacer > 0 ? React.createElement(Box, { height: virtualHistory.bottomSpacer }) : null
    )
  )
}

describe('useVirtualHistory offset cache reuse', () => {
  it('includes viewport height in the external-store snapshot key', () => {
    const base = {
      getPendingDelta: () => 0,
      getScrollTop: () => 20,
      isSticky: () => false
    }

    const short = virtualHistorySnapshotKey({
      ...base,
      getViewportHeight: () => 5
    } as ScrollBoxHandle)

    const tall = virtualHistorySnapshotKey({
      ...base,
      getViewportHeight: () => 25
    } as ScrollBoxHandle)

    expect(short).not.toBe(tall)
  })

  it('remounts enough tail rows after the scroll viewport grows', async () => {
    const items = Array.from({ length: 100 }, (_, index) => ({ height: 1, key: `item-${index}` }))
    const expose = { current: null as Exposed | null }
    const streams = makeStreams()

    const instance = renderSync(React.createElement(Harness, { expose, height: 4, items, maxMounted: 80 }), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    try {
      await delay(20)
      instance.rerender(React.createElement(Harness, { expose, height: 9, items, maxMounted: 80 }))
      await delay(80)

      expect(viewportIsMounted(items, expose.current!.virtualHistory, expose.current!.scroll!)).toBe(true)
    } finally {
      instance.unmount()
      instance.cleanup()
    }
  })

  it('recomputes tail coverage when wrapped rows shrink after a width resize', async () => {
    const items = Array.from({ length: 100 }, (_, index) => ({
      height: 4,
      heightAfterResize: 1,
      key: `item-${index}`
    }))

    const expose = { current: null as Exposed | null }
    const streams = makeStreams()

    const instance = renderSync(
      React.createElement(Harness, { columns: 40, expose, height: 10, items, maxMounted: 80 }),
      {
        patchConsole: false,
        stderr: streams.stderr as NodeJS.WriteStream,
        stdin: streams.stdin as NodeJS.ReadStream,
        stdout: streams.stdout as NodeJS.WriteStream
      }
    )

    try {
      await delay(20)
      instance.rerender(React.createElement(Harness, { columns: 80, expose, height: 10, items, maxMounted: 80 }))
      await delay(80)

      const resizedItems = items.map(item => ({ height: item.heightAfterResize!, key: item.key }))

      expect(viewportIsMounted(resizedItems, expose.current!.virtualHistory, expose.current!.scroll!)).toBe(true)
    } finally {
      instance.unmount()
      instance.cleanup()
    }
  })

  it('keeps sticky scroll at the bottom when one tall tail row resizes', async () => {
    const items = [{ height: 90, heightAfterResize: 50, key: 'tail' }]
    const expose = { current: null as Exposed | null }
    const streams = makeStreams()

    const instance = renderSync(
      React.createElement(Harness, { columns: 70, expose, height: 18, items, maxMounted: 80 }),
      {
        patchConsole: false,
        stderr: streams.stderr as NodeJS.WriteStream,
        stdin: streams.stdin as NodeJS.ReadStream,
        stdout: streams.stdout as NodeJS.WriteStream
      }
    )

    try {
      await delay(20)
      instance.rerender(React.createElement(Harness, { columns: 120, expose, height: 36, items, maxMounted: 80 }))
      await delay(80)

      const scroll = expose.current!.scroll!

      expect(scroll.getScrollTop()).toBe(scroll.getScrollHeight() - scroll.getViewportHeight())
    } finally {
      instance.unmount()
      instance.cleanup()
    }
  })

  it('recomputes offsets after a mounted row height changes', async () => {
    const tall = [
      { height: 6, key: 'a' },
      { height: 6, key: 'b' },
      { height: 6, key: 'c' }
    ]

    const short = tall.map(item => ({ ...item, height: 2 }))
    const expose = { current: null as Exposed | null }
    const streams = makeStreams()

    const instance = renderSync(React.createElement(Harness, { expose, items: tall }), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    try {
      await delay(20)
      expect(expose.current!.virtualHistory.offsets[tall.length]).toBe(18)

      instance.rerender(React.createElement(Harness, { expose, items: short }))
      await delay(40)

      expect(expose.current!.virtualHistory.offsets[short.length]).toBe(6)
      expect(expose.current!.virtualHistory.bottomSpacer).toBe(0)
    } finally {
      instance.unmount()
      instance.cleanup()
    }
  })

  it('ignores stale reused offset-array entries after the item count shrinks', async () => {
    const beforeShrink = Array.from({ length: 1400 }, (_, index) => ({ height: 1, key: `old${index}` }))
    const afterShrink = Array.from({ length: 800 }, (_, index) => ({ height: 7, key: `new${index}` }))
    const expose = { current: null as Exposed | null }
    const streams = makeStreams()

    const instance = renderSync(React.createElement(Harness, { expose, items: beforeShrink }), {
      patchConsole: false,
      stderr: streams.stderr as NodeJS.WriteStream,
      stdin: streams.stdin as NodeJS.ReadStream,
      stdout: streams.stdout as NodeJS.WriteStream
    })

    try {
      await delay(20)
      instance.rerender(React.createElement(Harness, { expose, items: afterShrink }))
      await delay(20)

      const scroll = expose.current!.scroll!
      const transcriptHeight = expose.current!.virtualHistory.offsets[afterShrink.length] ?? 0

      expect(transcriptHeight).toBe(5600)
      expect(scroll.getScrollTop()).toBe(transcriptHeight - scroll.getViewportHeight())

      scroll.scrollBy(-1)
      await delay(80)

      expect(scroll.getPendingDelta()).toBe(0)
      expect(viewportIsMounted(afterShrink, expose.current!.virtualHistory, scroll)).toBe(true)
    } finally {
      instance.unmount()
      instance.cleanup()
    }
  })

  it(
    'suppresses the positional follow-on-scroll snap after a manual scrollBy() ' +
      'scroll-up whose pending delta has not yet drained (issue #12884, review of #75439)',
    async () => {
      // scrollBy() does NOT move scrollTop immediately -- it only
      // accumulates a pendingScrollDelta, applied later during
      // virtualization's own drain cycle. So right after calling
      // scrollBy(-N), scrollTop can still read at the OLD max for a
      // render or two, satisfying the positional "at bottom"
      // predicate (scrollTopBeforeFollow >= prevMaxScroll) in
      // render-node-to-output.ts even though the user just initiated
      // an upward scroll. Without gating that predicate on
      // recentScrollUpTime, streaming content arriving in that window
      // immediately re-snaps to the new bottom, discarding the user's
      // still-pending scroll-up -- the exact #12884 symptom. This is
      // deliberately scrollBy(), not scrollTo() (which DOES move
      // scrollTop synchronously and so cannot reach this branch, per
      // the review's third finding).
      const initial = Array.from({ length: 30 }, (_, index) => ({ height: 2, key: `a${index}` }))
      const expose = { current: null as Exposed | null }
      const streams = makeStreams()

      const instance = renderSync(React.createElement(Harness, { expose, items: initial }), {
        patchConsole: false,
        stderr: streams.stderr as NodeJS.WriteStream,
        stdin: streams.stdin as NodeJS.ReadStream,
        stdout: streams.stdout as NodeJS.WriteStream
      })

      try {
        await delay(20)
        const scroll = expose.current!.scroll!
        const bottomBeforeScrollUp = scroll.getScrollTop()

        expect(bottomBeforeScrollUp).toBeGreaterThan(0)

        // Manual scroll-up: sets recentScrollUpTime synchronously, but
        // scrollTop itself has not moved yet (pendingScrollDelta only).
        scroll.scrollBy(-4)
        expect(scroll.getScrollTop()).toBe(bottomBeforeScrollUp)
        expect(scroll.getPendingDelta()).toBe(-4)

        // Streaming content arrives immediately after (well within the
        // 500ms grace window, and before the pending delta has drained):
        // scrollTopBeforeFollow still reads at the OLD max here, so
        // without the recentScrollUp gate this would satisfy the
        // positional "at bottom" check and snap to the NEW bottom,
        // discarding the pending upward delta entirely.
        const grown = [
          ...initial,
          ...Array.from({ length: 5 }, (_, index) => ({ height: 2, key: `b${index}` }))
        ]

        instance.rerender(React.createElement(Harness, { expose, items: grown }))
        await delay(20)

        const newBottom =
          (expose.current!.virtualHistory.offsets[grown.length] ?? 0) - scroll.getViewportHeight()

        expect(scroll.getScrollTop()).not.toBe(newBottom)
        expect(scroll.getScrollTop()).toBeLessThan(newBottom)

        // Past the 500ms grace window, the user explicitly scrolls back
        // to the (current) bottom themselves -- recentScrollUpTime has
        // expired, so the NEXT content growth correctly follows again.
        await delay(520)
        const currentBottom =
          (expose.current!.virtualHistory.offsets[grown.length] ?? 0) - scroll.getViewportHeight()

        scroll.scrollTo(Math.max(0, currentBottom))
        await delay(20)

        const grownMore = [
          ...grown,
          ...Array.from({ length: 5 }, (_, index) => ({ height: 2, key: `c${index}` }))
        ]

        instance.rerender(React.createElement(Harness, { expose, items: grownMore }))
        await delay(20)

        const finalTranscriptHeight = expose.current!.virtualHistory.offsets[grownMore.length] ?? 0
        const expectedBottom = Math.max(0, finalTranscriptHeight - scroll.getViewportHeight())

        expect(scroll.getScrollTop()).toBe(expectedBottom)
        expect(scroll.getScrollTop()).toBeGreaterThan(0)
      } finally {
        instance.unmount()
        instance.cleanup()
      }
    }
  )
})
