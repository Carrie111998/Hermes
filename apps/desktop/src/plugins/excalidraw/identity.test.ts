import { describe, expect, it } from 'vitest'

import {
  type ExcalidrawDocumentIdentity,
  excalidrawDocumentKey,
  excalidrawPaneId
} from './identity'

describe('Excalidraw document identity', () => {
  const identity: ExcalidrawDocumentIdentity = {
    path: '/drawings/design.excalidraw',
    profile: 'default',
    runtime: 'local'
  }

  it('distinguishes profile, runtime, and canonical path', () => {
    expect(excalidrawDocumentKey(identity)).not.toBe(
      excalidrawDocumentKey({ ...identity, profile: 'other' })
    )
    expect(excalidrawDocumentKey(identity)).not.toBe(
      excalidrawDocumentKey({ ...identity, runtime: 'remote:server' })
    )
    expect(excalidrawDocumentKey(identity)).not.toBe(
      excalidrawDocumentKey({ ...identity, path: '/drawings/other.excalidraw' })
    )
  })

  it('derives a stable pane id from the document identity', () => {
    expect(excalidrawPaneId(identity)).toBe(excalidrawPaneId(identity))
    expect(excalidrawPaneId(identity)).not.toBe(excalidrawPaneId({ ...identity, profile: 'other' }))
  })
})
