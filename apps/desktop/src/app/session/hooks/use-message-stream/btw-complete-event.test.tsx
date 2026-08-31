import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

const SID = 'btw-session'

let stream: MessageStreamHarness

const event = (type: string, payload: Record<string, unknown> = {}, session_id = SID) =>
  act(() => stream.handleEvent({ payload: { ...payload, timestamp: 900 }, session_id, type }))

describe('btw.complete event', () => {
  beforeEach(async () => {
    stream = renderMessageStream(SID)
  })

  afterEach(() => {
    cleanup()
  })

  it('renders the side-question answer as a persistent system message', () => {
    event('btw.complete', { question: 'which file failed?', task_id: 'btw_abc123', text: 'tests/test_x.py' })

    const system = stream.state(SID).messages.filter(m => m.role === 'system')
    const row = system.at(-1)

    expect(row).toBeDefined()
    expect(row?.parts.at(-1)).toMatchObject({ text: '💬 btw "which file failed?"\ntests/test_x.py' })
  })

  it('falls back to a bare header when the question is missing', () => {
    event('btw.complete', { task_id: 'btw_abc123', text: 'the answer' })

    const row = stream.state(SID).messages.filter(m => m.role === 'system').at(-1)

    expect(row?.parts.at(-1)).toMatchObject({ text: '💬 btw\nthe answer' })
  })

  it('drops an empty answer instead of seeding a blank row', () => {
    event('btw.complete', { task_id: 'btw_abc123', text: '' })

    expect(stream.state(SID).messages.filter(m => m.role === 'system')).toHaveLength(0)
  })
})
