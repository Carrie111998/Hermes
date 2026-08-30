import { afterEach, describe, expect, it } from 'vitest'

import {
  $threadJumpButtonVisible,
  $threadScrolledUp,
  publishThreadAtBottom,
  resetPublishedThreadScroll,
  resetThreadScroll,
  setThreadAtBottom
} from './thread-scroll'

afterEach(() => {
  resetThreadScroll()
})

describe('publishThreadAtBottom', () => {
  it('lets the visible pane flash the jump pill when the thread leaves the bottom', () => {
    publishThreadAtBottom(false, { paneVisible: true })

    expect($threadJumpButtonVisible.get()).toBe(true)
    expect($threadScrolledUp.get()).toBe(true)
  })

  it('ignores stick-to-bottom misses from a hidden keep-alive pane', () => {
    setThreadAtBottom(true)

    publishThreadAtBottom(false, { paneVisible: false })

    expect($threadJumpButtonVisible.get()).toBe(false)
    expect($threadScrolledUp.get()).toBe(false)
  })
})

describe('resetPublishedThreadScroll', () => {
  it('clears the jump pill when the visible pane unmounts', () => {
    setThreadAtBottom(false)

    resetPublishedThreadScroll({ paneVisible: true })

    expect($threadJumpButtonVisible.get()).toBe(false)
    expect($threadScrolledUp.get()).toBe(false)
  })

  it('does not clear the visible pane when a hidden list unmounts', () => {
    setThreadAtBottom(false)

    resetPublishedThreadScroll({ paneVisible: false })

    expect($threadJumpButtonVisible.get()).toBe(true)
    expect($threadScrolledUp.get()).toBe(true)
  })
})
