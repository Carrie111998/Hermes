import { act, cleanup, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as gatewayStore from '@/store/gateway'
import { clearAllPrompts } from '@/store/prompts'

import { type MessageStreamHarness, renderMessageStream } from './test-harness'

describe('approval gateway ownership', () => {
  afterEach(() => {
    cleanup()
    clearAllPrompts()
    vi.restoreAllMocks()
  })

  it('acknowledges a background approval through its source profile runtime', async () => {
    const request = vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockResolvedValue({ acknowledged: true })
    const stream: MessageStreamHarness = renderMessageStream('active-session')

    act(() =>
      stream.handleEvent({
        payload: { command: 'rm -rf /tmp/x', description: 'dangerous', request_id: 'req-1' },
        profile: 'worker',
        session_id: 'background-session',
        type: 'approval.request'
      })
    )

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(null, 'worker', 'approval.received', {
        request_id: 'req-1',
        session_id: 'background-session'
      })
    })
  })
})
