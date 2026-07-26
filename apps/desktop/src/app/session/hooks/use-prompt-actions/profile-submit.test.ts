/**
 * Layer 6c — profile-safe submission. Covers the SubmitExecution seam's
 * background path: profile-routed gateway, profile-scoped busy/awaiting that
 * never touch the foreground stores, the composite background state adapter,
 * session resolution + busy detection from real data sources, and the shared
 * submitTextForProfile entry point (route / queue-when-busy / no-session).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { listAllProfileSessions } from '@/hermes'
import { $busy } from '@/store/session'
import { requestGatewayForProfile, withProfileGatewayLease } from '@/store/gateway'
import { notify } from '@/store/notifications'
import {
  $profilePets,
  __resetPetMultiForTests,
  bindSessionStoredId,
  setSessionBusy
} from '@/store/pet-multi'
import { $activeGatewayProfile } from '@/store/profile'
import { $queuedPromptsBySession, profileQueueKey } from '@/store/composer-queue'

import {
  backgroundSubmitExecution,
  isProfileSessionBusy,
  resolveProfileSession,
  submitTextForProfile
} from './profile-submit'
import type { SubmitTextOptions } from './utils'

vi.mock('@/store/gateway', async importOriginal => {
  const actual = await importOriginal<typeof import('@/store/gateway')>()

  return {
    ...actual,
    requestGatewayForProfile: vi.fn(async () => ({})),
    withProfileGatewayLease: vi.fn(async (_profile: string, run: () => Promise<unknown>) => run())
  }
})

vi.mock('@/hermes', async importOriginal => {
  const actual = await importOriginal<typeof import('@/hermes')>()

  return { ...actual, listAllProfileSessions: vi.fn(async () => ({ sessions: [] })) }
})

vi.mock('@/store/notifications', () => ({
  notify: vi.fn()
}))

const listSessions = vi.mocked(listAllProfileSessions)
const profileRequest = vi.mocked(requestGatewayForProfile)
const lease = vi.mocked(withProfileGatewayLease)

const submitSpy = () => vi.fn(async (_text: string, _options?: SubmitTextOptions) => true)

beforeEach(() => {
  vi.clearAllMocks()
  __resetPetMultiForTests()
  $queuedPromptsBySession.set({})
  $activeGatewayProfile.set('default')
  $busy.set(false)
  listSessions.mockResolvedValue({ sessions: [] } as never)
})

describe('backgroundSubmitExecution (tests 26, 33, 55)', () => {
  it('is flagged background and names its profile', () => {
    const exec = backgroundSubmitExecution('Apollo')

    expect(exec.background).toBe(true)
    // normalizeProfileKey trims but preserves case.
    expect(exec.profile).toBe('Apollo')
    expect(exec.readBusy()).toBe(false)
  })

  it('routes every gateway call through requestGatewayForProfile with the profile (test 33)', async () => {
    const exec = backgroundSubmitExecution('apollo')

    await exec.requestGateway('prompt.submit', { session_id: 's1', text: 'hi' }, 1000)

    expect(profileRequest).toHaveBeenCalledWith(
      'apollo',
      'prompt.submit',
      { profile: 'apollo', session_id: 's1', text: 'hi' },
      1000,
      undefined
    )
  })

  it('drives the profile busy pose, never the foreground $busy (test 26)', () => {
    const exec = backgroundSubmitExecution('apollo')

    exec.scope.setBusy(true)
    exec.scope.setAwaitingResponse(true)

    expect($profilePets.get().get('apollo')?.activity.busy).toBe(true)
    // Foreground composer busy is untouched by a background submit.
    expect($busy.get()).toBe(false)

    exec.scope.setBusy(false)
    exec.scope.setAwaitingResponse(false)
    expect($profilePets.get().get('apollo')?.activity.busy).toBe(false)
  })

  it('runs optimistic state through the composite background adapter, not the foreground cache (test 55)', () => {
    const exec = backgroundSubmitExecution('apollo')

    const next = exec.updateSessionState(
      'rt-1',
      state => ({ ...state, busy: true, messages: [...state.messages, { id: 'user-1', parts: [], role: 'user' }] }),
      'stored-1'
    )

    // The adapter returns the updated background state...
    expect(next.busy).toBe(true)
    expect(next.messages.some(m => m.id === 'user-1')).toBe(true)
    expect(next.storedSessionId).toBe('stored-1')
    // ...and never paints the foreground view.
    expect(exec.scope.readAttachments()).toEqual([])
  })
})

describe('isProfileSessionBusy (test 58)', () => {
  it('reads busy from per-session activity by runtime id', () => {
    setSessionBusy('apollo', 'rt-1', true)

    expect(isProfileSessionBusy('apollo', 'rt-1', null)).toBe(true)
    expect(isProfileSessionBusy('apollo', 'rt-other', null)).toBe(false)
  })

  it('reads busy by stored id (survives runtime rotation)', () => {
    setSessionBusy('apollo', 'rt-1', true)
    bindSessionStoredId('apollo', 'rt-1', 'stored-1')

    expect(isProfileSessionBusy('apollo', null, 'stored-1')).toBe(true)
    expect(isProfileSessionBusy('apollo', null, 'stored-other')).toBe(false)
  })

  it('is scoped per profile', () => {
    setSessionBusy('apollo', 'rt-1', true)

    expect(isProfileSessionBusy('nova', 'rt-1', null)).toBe(false)
  })
})

describe('resolveProfileSession (tests 35, 58)', () => {
  it('returns the most-recent non-busy durable id from the cross-profile listing', async () => {
    listSessions.mockResolvedValue({
      sessions: [
        { id: 'stored-busy', profile: 'apollo' },
        { id: 'stored-free', profile: 'apollo' }
      ]
    } as never)
    setSessionBusy('apollo', 'rt-busy', true)
    bindSessionStoredId('apollo', 'rt-busy', 'stored-busy')

    const target = await resolveProfileSession('apollo', { excludeBusy: true })

    expect(target.storedSessionId).toBe('stored-free')
    expect(target.sessionId).toBeNull()
    // The listing is scoped to the named profile.
    expect(listSessions).toHaveBeenCalledWith(200, 1, 'exclude', 'recent', 'apollo')
  })

  it('returns the busy session when excludeBusy is false', async () => {
    listSessions.mockResolvedValue({ sessions: [{ id: 'stored-busy', profile: 'apollo' }] } as never)
    setSessionBusy('apollo', 'rt-busy', true)
    bindSessionStoredId('apollo', 'rt-busy', 'stored-busy')

    const target = await resolveProfileSession('apollo', { excludeBusy: false })

    expect(target.storedSessionId).toBe('stored-busy')
  })

  it('returns empty when the profile has no sessions', async () => {
    const target = await resolveProfileSession('apollo', { excludeBusy: true })

    expect(target).toEqual({})
  })
})

describe('submitTextForProfile (tests 11, 12, 35)', () => {
  it('runs a background profile through the SubmitExecution seam (test 11/33)', async () => {
    const submitText = submitSpy()

    const ok = await submitTextForProfile('apollo', 'hello', { storedSessionId: 'stored-1' }, submitText)

    expect(ok).toBe(true)
    expect(lease).toHaveBeenCalledWith('apollo', expect.any(Function))
    const options = submitText.mock.calls[0]?.[1]

    expect(options?.storedSessionId).toBe('stored-1')
    expect(options?.execution?.background).toBe(true)
    expect(options?.execution?.profile).toBe('apollo')
  })

  it('uses the ordinary foreground execution for the active profile', async () => {
    const submitText = submitSpy()

    await submitTextForProfile('default', 'hello', { storedSessionId: 'stored-1' }, submitText)

    expect(submitText.mock.calls[0]?.[1]?.execution).toBeUndefined()
  })

  it('passes the runtime + durable ids through when the caller has them', async () => {
    const submitText = submitSpy()

    await submitTextForProfile('apollo', 'hi', { sessionId: 'rt-1', storedSessionId: 'stored-1' }, submitText)

    const options = submitText.mock.calls[0]?.[1]

    expect(options?.sessionId).toBe('rt-1')
    expect(options?.storedSessionId).toBe('stored-1')
  })

  it('enqueues under (profile, storedSessionId) when the session is busy (test 12)', async () => {
    setSessionBusy('apollo', 'rt-1', true)
    bindSessionStoredId('apollo', 'rt-1', 'stored-1')
    const submitText = submitSpy()

    const ok = await submitTextForProfile('apollo', 'queued', { storedSessionId: 'stored-1' }, submitText)

    expect(ok).toBe(true)
    expect(submitText).not.toHaveBeenCalled()

    const queued = $queuedPromptsBySession.get()[profileQueueKey('apollo', 'stored-1')]

    expect(queued).toHaveLength(1)
    expect(queued[0]?.text).toBe('queued')
    expect(queued[0]?.profile).toBe('apollo')
  })

  it('cannot queue a busy session that has no durable id', async () => {
    setSessionBusy('apollo', 'rt-1', true)
    const submitText = submitSpy()

    const ok = await submitTextForProfile('apollo', 'x', { sessionId: 'rt-1' }, submitText)

    expect(ok).toBe(false)
    expect(submitText).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalled()
  })

  it('falls back to the most-recent non-busy session when the caller has none (test 35)', async () => {
    listSessions.mockResolvedValue({ sessions: [{ id: 'stored-recent', profile: 'apollo' }] } as never)
    const submitText = submitSpy()

    const ok = await submitTextForProfile('apollo', 'hi', {}, submitText)

    expect(ok).toBe(true)
    expect(submitText.mock.calls[0]?.[1]?.storedSessionId).toBe('stored-recent')
  })

  it('notifies and returns false when there is no session to target', async () => {
    const submitText = submitSpy()

    const ok = await submitTextForProfile('apollo', 'hi', {}, submitText)

    expect(ok).toBe(false)
    expect(submitText).not.toHaveBeenCalled()
    expect(notify).toHaveBeenCalled()
  })
})
