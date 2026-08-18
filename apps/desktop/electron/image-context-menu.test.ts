import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  clickComposerAttachmentRemoveScript,
  composerAttachmentIdAtPointScript,
  imageContextMenuItems,
  resolveComposerAttachmentRemove
} from './image-context-menu'

function createActions(extra = {}) {
  const calls = {
    copyImageAt: [],
    openImage: [],
    copyImageAddress: [],
    saveImage: [],
    removeAttachment: 0
  }

  return {
    calls,
    actions: {
      copyImageAt: (x, y) => calls.copyImageAt.push([x, y]),
      openImage: url => calls.openImage.push(url),
      copyImageAddress: url => calls.copyImageAddress.push(url),
      saveImage: url => calls.saveImage.push(url),
      ...extra
    }
  }
}

function menuLabels(items) {
  return items.map(item => (item.type === 'separator' ? '---' : item.label))
}

test('keeps Copy Image available when Chromium omits a large image srcURL', () => {
  const { actions, calls } = createActions()

  const items = imageContextMenuItems(
    { mediaType: 'image', hasImageContents: true, srcURL: '', x: 100, y: 120 },
    actions
  )

  assert.deepEqual(menuLabels(items), ['Copy Image'])

  items[0].click()
  assert.deepEqual(calls.copyImageAt, [[100, 120]])
})

test('keeps URL-dependent image actions when srcURL is available', () => {
  const { actions, calls } = createActions()
  const url = 'https://example.com/image.png'

  const items = imageContextMenuItems({ mediaType: 'image', hasImageContents: true, srcURL: url, x: 5, y: 8 }, actions)

  assert.deepEqual(menuLabels(items), ['Open Image', 'Copy Image', 'Copy Image Address', 'Save Image As...'])

  items[0].click()
  items[1].click()
  items[2].click()
  items[3].click()

  assert.deepEqual(calls.openImage, [url])
  assert.deepEqual(calls.copyImageAt, [[5, 8]])
  assert.deepEqual(calls.copyImageAddress, [url])
  assert.deepEqual(calls.saveImage, [url])
})

test('puts Remove Attachment first when the image is a composer chip', () => {
  const { actions, calls } = createActions({
    removeAttachment: () => {
      calls.removeAttachment += 1
    }
  })
  const url = 'https://example.com/image.png'

  const items = imageContextMenuItems({ mediaType: 'image', hasImageContents: true, srcURL: url, x: 5, y: 8 }, actions)

  assert.deepEqual(menuLabels(items), [
    'Remove Attachment',
    '---',
    'Open Image',
    'Copy Image',
    'Copy Image Address',
    'Save Image As...'
  ])

  items[0].click()
  assert.equal(calls.removeAttachment, 1)
  assert.deepEqual(calls.openImage, [])
})

test('does not add image actions for a non-image target', () => {
  const { actions } = createActions({ removeAttachment: () => undefined })

  assert.deepEqual(
    imageContextMenuItems({ mediaType: 'none', hasImageContents: false, srcURL: '', x: 0, y: 0 }, actions),
    []
  )
})

test('does not offer Copy Image when the target has no decoded image contents', () => {
  const { actions } = createActions()

  assert.deepEqual(
    imageContextMenuItems({ mediaType: 'image', hasImageContents: false, srcURL: '', x: 0, y: 0 }, actions),
    []
  )
})

test('composerAttachmentIdAtPointScript reads the chip under the cursor', () => {
  assert.match(composerAttachmentIdAtPointScript(12, 34), /elementFromPoint\(12, 34\)/)
  assert.equal(composerAttachmentIdAtPointScript(Number.NaN, 0), 'null')
})

test('clickComposerAttachmentRemoveScript targets the chip remove control', () => {
  const script = clickComposerAttachmentRemoveScript('image:shot.png')

  assert.match(script, /document\.querySelector/)
  assert.match(script, /composer-attachment-remove/)
  assert.match(script, /image:shot\.png/)
})

test('resolveComposerAttachmentRemove clicks the matching chip remove control', async () => {
  const scripts = []
  const webContents = {
    executeJavaScript: async script => {
      scripts.push(script)

      if (script.includes('elementFromPoint')) {
        return 'image:shot.png'
      }

      return undefined
    }
  }

  const remove = await resolveComposerAttachmentRemove(webContents, { mediaType: 'image', x: 10, y: 20 }, () => false)

  assert.equal(typeof remove, 'function')
  remove()
  assert.equal(scripts.length, 2)
  assert.match(scripts[1], /composer-attachment-remove/)
  assert.match(scripts[1], /image:shot\.png/)
})

test('resolveComposerAttachmentRemove ignores transcript images and destroyed windows', async () => {
  const webContents = {
    executeJavaScript: async () => null
  }

  assert.equal(
    await resolveComposerAttachmentRemove(webContents, { mediaType: 'image', x: 1, y: 1 }, () => false),
    undefined
  )
  assert.equal(
    await resolveComposerAttachmentRemove(webContents, { mediaType: 'image', x: 1, y: 1 }, () => true),
    undefined
  )
  assert.equal(
    await resolveComposerAttachmentRemove(webContents, { mediaType: 'none', x: 1, y: 1 }, () => false),
    undefined
  )
})
