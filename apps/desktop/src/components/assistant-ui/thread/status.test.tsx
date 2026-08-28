import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { __resetElapsedTimerRegistryForTests } from '@/components/chat/activity-timer'
import { clearClarifyRequest, setClarifyRequest } from '@/store/clarify'
import { I18nProvider } from '@/i18n'
import { $providerWaitSessions, setSessionProviderWait } from '@/store/provider-wait'
import { setSessionCompacting } from '@/store/compaction'
import { $activeSessionId, $turnStartedAt, setBusy } from '@/store/session'

import { resolveThreadActivityPhase, ResponseLoadingIndicator } from './status'

function renderIndicator() {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <ResponseLoadingIndicator />
    </I18nProvider>
  )
}

describe('ResponseLoadingIndicator timer', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-01-01T00:00:00.000Z'))
    // useViewedInterval gates ticking on document focus + visibility; jsdom's
    // hasFocus() is unreliable across runners, so pin it (same as the
    // background-sync backstop tests).
    vi.spyOn(globalThis.document, 'hasFocus').mockReturnValue(true)
    __resetElapsedTimerRegistryForTests()
  })

  afterEach(() => {
    cleanup()
    $activeSessionId.set(null)
    $turnStartedAt.set(null)
    $providerWaitSessions.set({})
    __resetElapsedTimerRegistryForTests()
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('preserves each running session timer while switching between sessions', () => {
    $activeSessionId.set('session-a')
    $turnStartedAt.set(Date.now())
    const sessionA = renderIndicator()

    act(() => vi.advanceTimersByTime(5_000))
    expect(screen.getAllByText((_, node) => node?.textContent === '5s').length).toBeGreaterThan(0)
    sessionA.unmount()

    $activeSessionId.set('session-b')
    $turnStartedAt.set(Date.now())
    const sessionB = renderIndicator()

    act(() => vi.advanceTimersByTime(3_000))
    expect(screen.getAllByText((_, node) => node?.textContent === '3s').length).toBeGreaterThan(0)
    sessionB.unmount()

    $activeSessionId.set('session-a')
    $turnStartedAt.set(new Date('2026-01-01T00:00:00.000Z').getTime())
    renderIndicator()

    expect(screen.getAllByText((_, node) => node?.textContent === '8s').length).toBeGreaterThan(0)
  })

  it('names a prolonged provider wait in the existing response status row', () => {
    $activeSessionId.set('session-a')
    $turnStartedAt.set(Date.now())
    setBusy(() => true)
    setSessionProviderWait('session-a', '⏳ waiting on local-model — 30s with no output yet')

    renderIndicator()

    expect(screen.getByText('⏳ waiting on local-model — 30s with no output yet')).toBeTruthy()
  })
})

// The status line sits between tool rows and thinking headers, which the
// transcript rests at a fade. Without the mark it reads a shade brighter than
// both — the one line in the column claiming emphasis it hasn't earned.
describe('status line', () => {
  afterEach(cleanup)

  it('is marked as transcript scaffolding', () => {
    $activeSessionId.set('session-a')
    $turnStartedAt.set(Date.now())
    const { container } = renderIndicator()

    expect(container.querySelector('[role="status"]')?.hasAttribute('data-conversation-scaffold')).toBe(true)
  })
})

describe('resolveThreadActivityPhase', () => {
  const base = {
    awaitingInput: false,
    busy: true,
    compacting: false,
    providerWait: '',
    quiet: false,
    stalled: false
  }

  it('prioritizes user input over every other active signal', () => {
    expect(resolveThreadActivityPhase({ ...base, awaitingInput: true, compacting: true, stalled: true })).toBe('input-required')
  })

  it('keeps provider wording instead of guessing a wait deadline', () => {
    expect(resolveThreadActivityPhase({ ...base, providerWait: 'waiting on local-model' })).toBe('provider-wait')
  })

  it('distinguishes compaction, stalled, quiet, and ordinary running work', () => {
    expect(resolveThreadActivityPhase({ ...base, compacting: true })).toBe('compacting')
    expect(resolveThreadActivityPhase({ ...base, stalled: true })).toBe('stalled')
    expect(resolveThreadActivityPhase({ ...base, quiet: true })).toBe('quiet-running')
    expect(resolveThreadActivityPhase(base)).toBe('running')
  })

  it('does not claim a running phase for an idle session', () => {
    expect(resolveThreadActivityPhase({ ...base, busy: false })).toBe('idle')
  })

  it('masks provider wait text while compaction is in progress', () => {
    expect(
      resolveThreadActivityPhase({
        ...base,
        compacting: true,
        providerWait: 'waiting on local-model'
      })
    ).toBe('compacting')
  })
})
describe('status hint', () => {
  afterEach(() => {
    cleanup()
    clearClarifyRequest()
    setSessionCompacting('session-a', false)
    setBusy(() => false)
    $activeSessionId.set(null)
    $turnStartedAt.set(null)
    $providerWaitSessions.set({})
  })

  it('uses input-required copy when input is requested during compaction', () => {
    $activeSessionId.set('session-a')
    $turnStartedAt.set(Date.now())
    setBusy(() => true)
    setSessionCompacting('session-a', true)
    setClarifyRequest({ choices: null, multiSelect: false, question: 'q', requestId: 'req-a', sessionId: 'session-a' })

    renderIndicator()

    const status = screen.getByRole('status')
    expect(status.getAttribute('aria-label')).toBe('Needs your input')
    expect(status.getAttribute('aria-label')).not.toBe('Summarizing thread')
    expect(status.querySelector('.shimmer')).toBeNull()
  })

  it('ignores whitespace-only provider wait in the status hint', () => {
    $activeSessionId.set('session-a')
    $turnStartedAt.set(Date.now())
    setBusy(() => true)
    setSessionProviderWait('session-a', '   ')

    renderIndicator()

    const status = screen.getByRole('status')
    expect(status.getAttribute('aria-label')).toBe('Hermes is loading a response')
  })
})
