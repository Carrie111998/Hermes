import { describe, expect, it } from 'vitest'

import {
  $sessionsLimit,
  bumpSessionsLimit,
  resetSessionsLimit,
  SIDEBAR_FILTERED_PAGE_SIZE,
  SIDEBAR_SESSIONS_PAGE_SIZE
} from './layout'

describe('sidebar session page size', () => {
  it('preserves the upstream page windows and keeps load-more incremental', () => {
    resetSessionsLimit()

    expect(SIDEBAR_SESSIONS_PAGE_SIZE).toBe(50)
    expect(SIDEBAR_FILTERED_PAGE_SIZE).toBe(300)
    expect($sessionsLimit.get()).toBe(50)

    bumpSessionsLimit()
    expect($sessionsLimit.get()).toBe(100)
  })
})
