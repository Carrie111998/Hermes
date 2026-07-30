import { describe, expect, it, vi } from 'vitest'

import { connectGatewayWithBootRetries } from './connect-gateway-with-boot-retries'

describe('connectGatewayWithBootRetries', () => {
  it('returns on the first successful connect', async () => {
    const connect = vi.fn(async () => undefined)
    const sleep = vi.fn(async () => undefined)

    await connectGatewayWithBootRetries(connect, 'ws://local/api/ws', { sleep })

    expect(connect).toHaveBeenCalledTimes(1)
    expect(sleep).not.toHaveBeenCalled()
  })

  it('retries transient failures then succeeds without surfacing the early errors', async () => {
    const connect = vi
      .fn()
      .mockRejectedValueOnce(new Error('Could not connect to Hermes gateway'))
      .mockResolvedValueOnce(undefined)
    const sleep = vi.fn(async () => undefined)

    await connectGatewayWithBootRetries(connect, 'ws://local/api/ws', {
      attempts: 3,
      delayMs: 10,
      sleep
    })

    expect(connect).toHaveBeenCalledTimes(2)
    expect(sleep).toHaveBeenCalledTimes(1)
  })

  it('throws the last error after exhausting attempts', async () => {
    const connect = vi.fn(async () => {
      throw new Error('Could not connect to Hermes gateway')
    })
    const sleep = vi.fn(async () => undefined)

    await expect(
      connectGatewayWithBootRetries(connect, 'ws://local/api/ws', {
        attempts: 3,
        delayMs: 5,
        sleep
      })
    ).rejects.toThrow('Could not connect to Hermes gateway')

    expect(connect).toHaveBeenCalledTimes(3)
    expect(sleep).toHaveBeenCalledTimes(2)
  })

  it('stops retrying once cancelled', async () => {
    let cancelled = false
    const connect = vi.fn(async () => {
      cancelled = true
      throw new Error('Could not connect to Hermes gateway')
    })
    const sleep = vi.fn(async () => undefined)

    await connectGatewayWithBootRetries(connect, 'ws://local/api/ws', {
      attempts: 5,
      isCancelled: () => cancelled,
      sleep
    })

    expect(connect).toHaveBeenCalledTimes(1)
    expect(sleep).not.toHaveBeenCalled()
  })
})
