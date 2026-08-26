import { act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import {
  $currentCwd,
  releaseWorkspaceCwdOwner,
  setCurrentCwd,
  setSelectedStoredSessionId,
  setWorkspaceCwdOwner,
  workspaceCwdBelongsToSelectedSession
} from '@/store/session'

import { renderMessageStream } from './test-harness'

const RUNTIME_ID = 'runtime-active'
const STORED_ID = 'stored-active'

describe('live session.info workspace reconciliation', () => {
  beforeEach(() => {
    setSelectedStoredSessionId(STORED_ID)
    setCurrentCwd('/previous/project')
    setWorkspaceCwdOwner(STORED_ID)
  })

  afterEach(() => {
    cleanup()
    setSelectedStoredSessionId(null)
    setCurrentCwd('')
    releaseWorkspaceCwdOwner()
  })

  it('caches a neutral execution cwd as detached and releases the selected workspace', () => {
    const state = createClientSessionState(STORED_ID)

    const stream = renderMessageStream(RUNTIME_ID, {
      states: new Map([[RUNTIME_ID, state]])
    })

    act(() =>
      stream.handleEvent({
        payload: {
          cwd: '/home/backend/.hermes/workspace',
          cwd_owned: false,
          stored_session_id: STORED_ID
        },
        session_id: RUNTIME_ID,
        type: 'session.info'
      })
    )

    expect($currentCwd.get()).toBe('/previous/project')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(false)
    expect(stream.state()).toMatchObject({ cwd: '', cwdOwned: false })
  })

  it('keeps legacy non-empty cwd events owned when cwd_owned is absent', () => {
    const state = createClientSessionState(STORED_ID)

    const stream = renderMessageStream(RUNTIME_ID, {
      states: new Map([[RUNTIME_ID, state]])
    })

    act(() =>
      stream.handleEvent({
        payload: { cwd: '/legacy/project', stored_session_id: STORED_ID },
        session_id: RUNTIME_ID,
        type: 'session.info'
      })
    )

    expect($currentCwd.get()).toBe('/legacy/project')
    expect(workspaceCwdBelongsToSelectedSession()).toBe(true)
    expect(stream.state()).toMatchObject({ cwd: '/legacy/project', cwdOwned: true })
  })
})
