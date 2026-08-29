import { act, cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { parseMoaProgress } from '@/lib/moa-progress'
import type { RpcEvent } from '@/types/hermes'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const SID = 'session-1'
let stream: MessageStreamHarness

function mountStream() {
  stream = renderMessageStream(SID)
}

function emit(type: RpcEvent['type'], payload: RpcEvent['payload'] = {}) {
  act(() => stream.handleEvent({ payload, session_id: SID, type }))
}

function state() {
  const parsed = parseMoaProgress(stream.reasoningText())

  expect(parsed).not.toBeNull()

  return parsed!
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('structured MoA orchestration stream', () => {
  it('shows the stable roster before completion and updates out of order in place', () => {
    mountStream()
    emit('message.start')
    emit('moa.phase', {
      advisors: Array.from({ length: 10 }, (_, index) => `model-${index + 1}`),
      aggregator: 'agg-model',
      concurrency: 8,
      fanout: 'user_turn',
      guidance_reused: false,
      phase: 'reference',
      refs_done: 0,
      refs_total: 10
    })

    expect(state().advisors.map(advisor => advisor.status)).toEqual([
      'running',
      'running',
      'running',
      'running',
      'running',
      'running',
      'running',
      'running',
      'queued',
      'queued'
    ])

    emit('moa.progress', { index: 4, label: 'model-4', refs_done: 1, refs_total: 10, status: 'complete' })

    const afterFourth = state()
    expect(afterFourth.advisors[3].status).toBe('complete')
    expect(afterFourth.advisors[0].status).toBe('running')
    expect(afterFourth.advisors[8].status).toBe('running')
    expect(afterFourth.advisors[9].status).toBe('queued')

    emit('moa.progress', { index: 2, label: 'model-2', refs_done: 2, refs_total: 10, status: 'failed' })
    expect(state().advisors[1].status).toBe('failed')
  })

  it('keeps advisor output behind its slot and transitions waiting → acting → settled', () => {
    mountStream()
    emit('message.start')
    emit('moa.phase', {
      advisors: ['model-a', 'model-b'],
      aggregator: 'agg-model',
      concurrency: 2,
      fanout: 'per_iteration',
      guidance_reused: false,
      phase: 'reference',
      refs_done: 0,
      refs_total: 2
    })
    emit('moa.progress', { index: 2, label: 'model-b', refs_done: 1, refs_total: 2, status: 'complete' })
    emit('moa.reference', { count: 2, index: 2, label: 'model-b', text: 'second finished first' })

    expect(state().phase).toBe('reference')
    expect(state().advisors[1]).toMatchObject({ output: 'second finished first', status: 'complete' })

    emit('moa.progress', { index: 1, label: 'model-a', refs_done: 2, refs_total: 2, status: 'interrupted' })
    emit('moa.reference', { count: 2, index: 1, label: 'model-a', text: '[skipped: interrupted by user]' })
    emit('moa.phase', {
      aggregator: 'agg-model',
      guidance_reused: false,
      phase: 'aggregator',
      refs_done: 2,
      refs_total: 2
    })

    expect(state().phase).toBe('aggregating')
    expect(state().advisors[0].status).toBe('interrupted')

    emit('message.complete', { text: 'final answer' })
    expect(state().phase).toBe('settled')
  })

  it('distinguishes cached guidance reuse from a fresh fan-out', () => {
    mountStream()
    emit('message.start')
    emit('moa.phase', {
      advisors: ['model-a', 'model-b'],
      aggregator: 'agg-model',
      concurrency: 2,
      fanout: 'every_n:3',
      guidance_reused: true,
      phase: 'reference',
      refs_done: 2,
      refs_total: 2
    })

    expect(state()).toMatchObject({ fanout: 'every_n:3', guidanceReused: true, phase: 'reference' })
    expect(state().advisors.every(advisor => advisor.status === 'complete')).toBe(true)
  })
})
