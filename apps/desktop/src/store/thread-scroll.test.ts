import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  notifyThreadEditOpen,
  onScrollToBottomRequest,
  onThreadEditOpen,
  requestScrollToBottom,
  resetThreadScroll,
  setThreadAtBottom,
  threadJumpButtonVisibleStore,
  threadScrolledUpStore
} from './thread-scroll'

afterEach(() => {
  resetThreadScroll('tile-a')
  resetThreadScroll('tile-b')
  resetThreadScroll()
})

describe('thread scroll isolation', () => {
  it('requestScrollToBottom only fires the handler for that surface id', () => {
    const tileA = vi.fn()
    const tileB = vi.fn()
    const stopA = onScrollToBottomRequest(tileA, 'tile-a')
    const stopB = onScrollToBottomRequest(tileB, 'tile-b')

    requestScrollToBottom('tile-a')

    expect(tileA).toHaveBeenCalledOnce()
    expect(tileB).not.toHaveBeenCalled()

    requestScrollToBottom('tile-b')

    expect(tileA).toHaveBeenCalledOnce()
    expect(tileB).toHaveBeenCalledOnce()

    stopA()
    stopB()
  })

  it('keeps scrolled-up state per surface so one tile cannot dim another', () => {
    setThreadAtBottom(false, 'tile-a')

    expect(threadScrolledUpStore('tile-a').get()).toBe(true)
    expect(threadJumpButtonVisibleStore('tile-a').get()).toBe(true)
    expect(threadScrolledUpStore('tile-b').get()).toBe(false)
    expect(threadJumpButtonVisibleStore('tile-b').get()).toBe(false)
  })

  it('does not broadcast an inline-edit hold to other tiles', () => {
    const tileA = vi.fn()
    const tileB = vi.fn()
    const stopA = onThreadEditOpen(tileA, 'tile-a')
    const stopB = onThreadEditOpen(tileB, 'tile-b')

    notifyThreadEditOpen('tile-a')

    expect(tileA).toHaveBeenCalledOnce()
    expect(tileB).not.toHaveBeenCalled()

    stopA()
    stopB()
  })
})
