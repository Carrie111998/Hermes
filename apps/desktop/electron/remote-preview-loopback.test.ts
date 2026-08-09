import assert from 'node:assert/strict'

import { test } from 'vitest'

import { normalizeRemotePreviewTarget } from './remote-preview-target'
import { remotePreviewTargetForForwarding } from './ssh-preview-forwarding'

test('does not treat localhost trailing dots as public on an allowed port', async () => {
  const target = 'http://localhost.:5173/'

  assert.equal(await remotePreviewTargetForForwarding(target, true, undefined), null)
  assert.equal(normalizeRemotePreviewTarget(target, target), null)
})

test('requires forwarding for every IPv4 loopback address on an allowed port', async () => {
  const target = 'http://127.0.0.2:5173/'

  assert.equal(await remotePreviewTargetForForwarding(target, true, undefined), null)
  assert.equal(normalizeRemotePreviewTarget(target, target), null)
})

test('requires forwarding for IPv4-mapped IPv6 loopback on an allowed port', async () => {
  const target = 'http://[::ffff:127.0.0.1]:5173/'

  assert.equal(await remotePreviewTargetForForwarding(target, true, undefined), null)
  assert.equal(normalizeRemotePreviewTarget(target, target), null)
})

test('requires forwarding for the IPv6 unspecified address on an allowed port', async () => {
  const target = 'http://[::]:5173/'

  assert.equal(await remotePreviewTargetForForwarding(target, true, undefined), null)
  assert.equal(normalizeRemotePreviewTarget(target, target), null)
})

test('keeps a public HTTPS URL unchanged', async () => {
  const target = 'https://example.com/docs?x=1#top'

  assert.equal(await remotePreviewTargetForForwarding(target, true, undefined), target)
  assert.deepEqual(normalizeRemotePreviewTarget(target, target), {
    kind: 'url',
    label: 'example.com/docs',
    source: target,
    url: target
  })
})
