import { beforeEach, describe, expect, it } from 'vitest'

import { $fileBrowserOpen, setFileBrowserOpen } from '@/store/layout'
import { $rightPanelOpen, setRightPanelOpen } from '@/store/right-panel'

import { toggleFilesPanel } from './actions'

describe('file panel toggle', () => {
  beforeEach(() => {
    setRightPanelOpen(true)
    setFileBrowserOpen(false)
  })

  it('opens and closes the independent file panel', () => {
    toggleFilesPanel()
    expect($fileBrowserOpen.get()).toBe(true)

    toggleFilesPanel()
    expect($fileBrowserOpen.get()).toBe(false)
  })

  it('reveals Files instead of disabling it when the whole right side is hidden', () => {
    setFileBrowserOpen(true)
    setRightPanelOpen(false)

    toggleFilesPanel()

    expect($fileBrowserOpen.get()).toBe(true)
    expect($rightPanelOpen.get()).toBe(true)
  })
})
