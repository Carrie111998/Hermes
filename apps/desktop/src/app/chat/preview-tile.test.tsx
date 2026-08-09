import { beforeEach, describe, expect, it, vi } from 'vitest'

import type * as TreeStore from '@/components/pane-shell/tree/store'

const { revealTreePane } = vi.hoisted(() => ({ revealTreePane: vi.fn() }))

vi.mock('@/components/pane-shell/tree/store', async importOriginal => {
  const actual = await importOriginal<typeof TreeStore>()

  return { ...actual, revealTreePane }
})

vi.mock('./pane-mirror', () => ({
  paneMirror: () => () => undefined
}))

import { $rightRailActiveTabId } from '@/store/layout'
import { $previewTabs, closeRightRail, type PreviewTarget } from '@/store/preview'

import { watchPreviewTiles } from './preview-tile'

const target: PreviewTarget = {
  kind: 'url',
  label: 'Browser',
  source: 'http://127.0.0.1:5173',
  url: 'http://127.0.0.1:48231/'
}

describe('watchPreviewTiles', () => {
  beforeEach(() => {
    closeRightRail()
    revealTreePane.mockClear()
  })

  it('reveals a restored active Browser tab during initial wiring', () => {
    $previewTabs.set([{ id: 'url:browser', target }])
    $rightRailActiveTabId.set('url:browser')

    watchPreviewTiles()

    expect(revealTreePane).toHaveBeenCalledWith('preview-tile:url:browser')
  })
})
