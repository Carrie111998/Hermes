import type { ThreadMessage } from '@assistant-ui/react'
import { cleanup, render, screen } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, describe, expect, it } from 'vitest'

import { PRIMARY_SESSION_VIEW, SessionViewProvider } from '@/app/chat/session-view'

import { stubThreadEnvironment, stubThreadViewportSize, ThreadRuntime, userMessage } from '../test-utils'

import { Thread } from '.'

stubThreadEnvironment()
stubThreadViewportSize()

afterEach(cleanup)

describe('user message delivery status', () => {
  it('labels the existing optimistic bubble as queued without rendering a duplicate message', async () => {
    const message = {
      ...userMessage('user-question-mark', '؟'),
      metadata: { custom: { deliveryState: 'queued' } }
    } as ThreadMessage

    const { container } = render(
      <ThreadRuntime messages={[message]}>
        <Thread cwd={null} gateway={null} sessionId="session-1" />
      </ThreadRuntime>
    )

    expect((await screen.findByText('Queued')).getAttribute('data-delivery-state')).toBe('queued')
    expect(container.querySelectorAll('[data-message-id="user-question-mark"]')).toHaveLength(1)
  })

  it('keeps delivery truth visible on every explicitly queued bubble', async () => {
    const first = {
      ...userMessage('user-first', 'انت بتعمل harness ليه'),
      metadata: { custom: { deliveryState: 'queued' } }
    } as ThreadMessage

    const second = {
      ...userMessage('user-second', 'احنا بقالنا يومين شغالين ولسه مخلصناش'),
      metadata: { custom: { deliveryState: 'queued' } }
    } as ThreadMessage

    const { container } = render(
      <ThreadRuntime messages={[first, second]}>
        <Thread cwd={null} gateway={null} sessionId="session-1" />
      </ThreadRuntime>
    )

    expect(await screen.findAllByText('Queued')).toHaveLength(2)
    expect(container.querySelectorAll('[data-delivery-state="queued"]')).toHaveLength(2)
    expect(container.querySelectorAll('[data-role="user"]')).toHaveLength(2)
  })

  it('projects a cross-process queued activity onto the latest visible user bubble', async () => {
    const queuedView = {
      ...PRIMARY_SESSION_VIEW,
      $lastActivityDescription: atom('Queued behind active Hermes — executing tool: terminal · waiting 30s.')
    }

    render(
      <SessionViewProvider value={queuedView}>
        <ThreadRuntime messages={[userMessage('user-question-mark', '؟')]}>
          <Thread cwd={null} gateway={null} sessionId="session-1" />
        </ThreadRuntime>
      </SessionViewProvider>
    )

    expect((await screen.findByText('Queued')).getAttribute('data-delivery-state')).toBe('queued')
  })
})
