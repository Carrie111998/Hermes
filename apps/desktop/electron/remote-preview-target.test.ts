import assert from 'node:assert/strict'

import { test } from 'vitest'

import { normalizeRemotePreviewTarget } from './remote-preview-target'

test('preserves a public URL when explicit remote forwarding is requested', () => {
  const raw = 'https://example.com/docs?x=1#top'

  assert.deepEqual(normalizeRemotePreviewTarget(raw, raw), {
    kind: 'url',
    label: 'example.com/docs',
    source: raw,
    url: raw
  })
})
