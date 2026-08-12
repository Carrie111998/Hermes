import { fireEvent, render, screen } from '@testing-library/react'
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import type { ClientSessionState } from '@/app/types'
import { findGroupOfPane, group } from '@/components/pane-shell/tree/model'
import { $layoutTree, declareDefaultTree, watchContributedPanes } from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { createClientSessionState } from '@/lib/chat-runtime'
import { excalidrawPaneId } from '@/plugins/excalidraw/identity'
import { $excalidrawDocuments, resetExcalidrawDocumentsForTest } from '@/plugins/excalidraw/store'
import { $activeSessionId } from '@/store/session'
import { dropSessionState, publishSessionState } from '@/store/session-states'

import { MarkdownTextContent } from './markdown-text'

const SESSION_ID = 'chat-runtime'

const scope = {
  cwd: '/workspace/project',
  profile: 'design',
  runtime: 'remote:ssh:design:workstation'
}

function scopedSessionState(): ClientSessionState {
  return {
    ...createClientSessionState('design/session-1'),
    ...scope
  } as ClientSessionState
}

describe('MarkdownTextContent Excalidraw file links', () => {
  let disposeWorkspace: () => void

  beforeAll(() => {
    disposeWorkspace = registry.register({
      area: 'panes',
      data: { placement: 'main', uncloseable: true },
      id: 'workspace',
      render: () => null,
      title: 'workspace'
    })
    watchContributedPanes()
  })

  beforeEach(() => {
    window.localStorage.clear()
    resetExcalidrawDocumentsForTest()
    declareDefaultTree(group(['workspace'], { id: 'grp-main' }))
    publishSessionState(SESSION_ID, scopedSessionState())
    $activeSessionId.set(SESSION_ID)
  })

  afterAll(() => {
    resetExcalidrawDocumentsForTest()
    dropSessionState(SESSION_ID)
    $activeSessionId.set(null)
    disposeWorkspace()
    window.localStorage.clear()
  })

  it('opens a scoped relative drawing link in the existing Excalidraw pane', async () => {
    render(<MarkdownTextContent isRunning={false} text="Open [system.excalidraw](diagrams/system.excalidraw)." />)

    fireEvent.click(await screen.findByRole('link', { name: 'system.excalidraw' }))

    const identity = {
      path: '/workspace/project/diagrams/system.excalidraw',
      profile: scope.profile,
      runtime: scope.runtime
    }

    const paneId = excalidrawPaneId(identity)
    const layout = $layoutTree.get()

    expect($excalidrawDocuments.get()).toEqual([{ fingerprint: '', identity, status: 'connected' }])
    expect(layout && findGroupOfPane(layout, paneId)?.active).toBe(paneId)
    expect(layout && findGroupOfPane(layout, paneId)?.minimized).not.toBe(true)
  })

  it('preserves an absolute drawing path and reuses its pane on repeated clicks', async () => {
    render(
      <MarkdownTextContent isRunning={false} text="Open [architecture.excalidraw](/shared/architecture.excalidraw)." />
    )

    const link = await screen.findByRole('link', { name: 'architecture.excalidraw' })
    fireEvent.click(link)
    fireEvent.click(link)

    expect($excalidrawDocuments.get()).toEqual([
      {
        fingerprint: '',
        identity: {
          path: '/shared/architecture.excalidraw',
          profile: scope.profile,
          runtime: scope.runtime
        },
        status: 'connected'
      }
    ])
  })

  it('does not claim a relative drawing when the transcript has no cwd', async () => {
    publishSessionState(SESSION_ID, { ...scopedSessionState(), cwd: '' })
    render(<MarkdownTextContent isRunning={false} text="Open [system.excalidraw](system.excalidraw)." />)

    expect(await screen.findByText(/system\.excalidraw/)).toBeTruthy()
    expect($excalidrawDocuments.get()).toHaveLength(0)
  })
  it('leaves an ordinary relative file on the existing blocked-link path', async () => {
    render(<MarkdownTextContent isRunning={false} text="Open [candidate](notes.txt)." />)

    expect(await screen.findByText(/candidate/)).toBeTruthy()
    expect($excalidrawDocuments.get()).toHaveLength(0)
  })

  it('leaves a remote drawing URL on the ordinary link path', async () => {
    render(<MarkdownTextContent isRunning={false} text="Open [candidate](https://example.com/system.excalidraw)." />)

    fireEvent.click(await screen.findByRole('link', { name: 'candidate' }))

    expect($excalidrawDocuments.get()).toHaveLength(0)
  })
})
