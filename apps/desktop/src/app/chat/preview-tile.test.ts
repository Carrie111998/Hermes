import { describe, expect, it } from 'vitest'

import { previewTileDock } from './preview-tile'

describe('preview tile default docking', () => {
  it('opens the first preview beside the workspace', () => {
    expect(previewTileDock('file:first', ['file:first'])).toEqual({
      anchor: 'workspace',
      dir: 'right'
    })
  })

  it('stacks later files as tabs in the existing preview group', () => {
    expect(previewTileDock('file:second', ['file:first', 'file:second'])).toEqual({
      anchor: 'preview-tile:file:first',
      dir: 'center'
    })
  })

  it('uses the oldest remaining preview as the tab-group anchor', () => {
    expect(previewTileDock('file:third', ['file:second', 'file:third'])).toEqual({
      anchor: 'preview-tile:file:second',
      dir: 'center'
    })
  })
})
