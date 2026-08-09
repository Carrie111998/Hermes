import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  createOrReplaceSshPreviewForwarder,
  createOrReuseSshPreviewForwarder,
  createSshPreviewForwarder,
  isLocalPreviewUrl,
  isRemotePreviewForwardingRequested,
  remotePreviewTargetForForwarding
} from './ssh-preview-forwarding'

test('gates SSH preview rewriting on the literal true flag', () => {
  assert.equal(isRemotePreviewForwardingRequested(true), true)
  assert.equal(isRemotePreviewForwardingRequested(false), false)
  assert.equal(isRemotePreviewForwardingRequested(undefined), false)
  assert.equal(isRemotePreviewForwardingRequested(1), false)
  assert.equal(isRemotePreviewForwardingRequested('true'), false)
})

test('fails closed when explicit remote forwarding cannot provide a rewrite', async () => {
  assert.equal(await remotePreviewTargetForForwarding('http://localhost:5173/', true, undefined), null)
  assert.equal(
    await remotePreviewTargetForForwarding(
      'http://localhost:5174/',
      true,
      createSshPreviewForwarder({
        pickLocalPort: async () => 45175,
        forward: async () => {},
        cancelForward: async () => {}
      })
    ),
    'http://127.0.0.1:45175/'
  )
  assert.equal(
    await remotePreviewTargetForForwarding('http://localhost:45176/', true, {
      rewrite: async () => null,
      close: async () => {}
    }),
    null
  )
  assert.equal(
    await remotePreviewTargetForForwarding('https://example.com/docs', true, undefined),
    'https://example.com/docs'
  )
  assert.equal(await remotePreviewTargetForForwarding('http://localhost:5173/', false, undefined), undefined)
})

test('rejects an unsupported remote preview port before invoking SSH', async () => {
  let forwards = 0

  const preview = createSshPreviewForwarder({
    pickLocalPort: async () => 45175,
    forward: async () => {
      forwards += 1
    },
    cancelForward: async () => {}
  })

  assert.equal(await remotePreviewTargetForForwarding('http://localhost:45173/app?x=1#top', true, preview), null)
  assert.equal(forwards, 0)
})

test('reuses an existing forwarder for a live SSH state', () => {
  const existing = createSshPreviewForwarder({
    pickLocalPort: async () => 44000,
    forward: async () => {},
    cancelForward: async () => {}
  })

  assert.equal(
    createOrReuseSshPreviewForwarder(existing, {
      pickLocalPort: async () => 44001,
      forward: async () => {},
      cancelForward: async () => {}
    }),
    existing
  )
})

test('closes an old forwarder before replacing it after dashboard recovery', async () => {
  const events: string[] = []

  const existing = {
    rewrite: async () => null,
    close: async () => {
      events.push('close')
    }
  }

  const replacement = await createOrReplaceSshPreviewForwarder(
    existing,
    {
      pickLocalPort: async () => {
        events.push('pick')

        return 45178
      },
      forward: async () => {
        events.push('forward')
      },
      cancelForward: async () => {}
    },
    false
  )

  assert.notEqual(replacement, existing)
  assert.equal(await replacement.rewrite('http://localhost:5173/'), 'http://127.0.0.1:45178/')
  assert.deepEqual(events, ['close', 'pick', 'forward'])
})

test('classifies only local HTTP(S) preview hosts and keeps the remote port', () => {
  assert.deepEqual(isLocalPreviewUrl('http://localhost:5173/app?x=1#top'), { remotePort: 5173 })
  assert.deepEqual(isLocalPreviewUrl('https://[::1]/app'), { remotePort: 443 })
  assert.deepEqual(isLocalPreviewUrl('http://0.0.0.0/app'), { remotePort: 80 })
  assert.equal(isLocalPreviewUrl('https://preview.example/app'), null)
  assert.equal(isLocalPreviewUrl('file:///tmp/app.html'), null)
})

test('rewrites a local preview while preserving path, query, and hash', async () => {
  const forward = createSshPreviewForwarder({
    pickLocalPort: async () => 44001,
    forward: async () => {},
    cancelForward: async () => {}
  })

  await assert.doesNotReject(async () => {
    assert.equal(await forward.rewrite('http://localhost:5173/app?a=1#top'), 'http://127.0.0.1:44001/app?a=1#top')
  })
})

test('reuses one local forward for repeated URLs to the same remote port', async () => {
  let picks = 0
  let forwards = 0

  const preview = createSshPreviewForwarder({
    pickLocalPort: async () => {
      picks += 1

      return 44002
    },
    forward: async () => {
      forwards += 1
    },
    cancelForward: async () => {}
  })

  assert.equal(await preview.rewrite('http://127.0.0.1:5173/one'), 'http://127.0.0.1:44002/one')
  assert.equal(
    await preview.rewrite('http://localhost:5173/two?tab=logs#bottom'),
    'http://127.0.0.1:44002/two?tab=logs#bottom'
  )
  assert.equal(picks, 1)
  assert.equal(forwards, 1)
})

test('retries bind collisions and cleans up the failed candidate', async () => {
  const picked = [44003, 44004]
  const forwarded: number[] = []
  const cancelled: number[] = []

  const preview = createSshPreviewForwarder({
    pickLocalPort: async () => picked.shift()!,
    forward: async localPort => {
      forwarded.push(localPort)

      if (localPort === 44003) {
        throw new Error('bind: address already in use')
      }
    },
    cancelForward: async localPort => {
      cancelled.push(localPort)
    }
  })

  assert.equal(await preview.rewrite('http://localhost:8080/'), 'http://127.0.0.1:44004/')
  assert.deepEqual(forwarded, [44003, 44004])
  assert.deepEqual(cancelled, [44003])
})

test('cleans up a failed non-collision forward before surfacing the error', async () => {
  const cancelled: number[] = []

  const preview = createSshPreviewForwarder({
    pickLocalPort: async () => 44005,
    forward: async () => {
      throw new Error('SSH connection dropped')
    },
    cancelForward: async localPort => {
      cancelled.push(localPort)
    }
  })

  await assert.rejects(() => preview.rewrite('http://localhost:8081/'), /SSH connection dropped/)
  assert.deepEqual(cancelled, [44005])
})

test('teardown cancels every preview forward and is idempotent', async () => {
  const cancelled: Array<[number, number]> = []
  let nextPort = 44006

  const preview = createSshPreviewForwarder({
    pickLocalPort: async () => nextPort++,
    forward: async () => {},
    cancelForward: async (localPort, remotePort) => {
      cancelled.push([localPort, remotePort])
    }
  })

  await preview.rewrite('http://localhost:5173/one')
  await preview.rewrite('http://localhost:5174/two')
  await preview.close()
  await preview.close()

  assert.deepEqual(cancelled, [
    [44006, 5173],
    [44007, 5174]
  ])
})
