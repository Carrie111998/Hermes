import { beforeEach, describe, expect, it } from 'vitest'

import {
  $browserFullscreenTabId,
  $previewTabs,
  activePreviewTabId,
  closeRightRail,
  exitBrowserFullscreen,
  openBrowserTab,
  toggleBrowserFullscreen
} from './preview'

beforeEach(() => {
  closeRightRail()
  $browserFullscreenTabId.set(null)
})

describe('browser fullscreen', () => {
  it('toggles a tab in and out', () => {
    toggleBrowserFullscreen('url:browser-1')
    expect($browserFullscreenTabId.get()).toBe('url:browser-1')

    toggleBrowserFullscreen('url:browser-1')
    expect($browserFullscreenTabId.get()).toBeNull()
  })

  // Only one page can fill the window; a second request takes it over rather
  // than leaving two panes both believing they are fullscreen.
  it('a second tab takes the window over', () => {
    toggleBrowserFullscreen('url:browser-1')
    toggleBrowserFullscreen('url:browser-2')

    expect($browserFullscreenTabId.get()).toBe('url:browser-2')
  })

  // The close path passes the id of the tab going away — it must not drop
  // another tab's fullscreen along with it.
  it('exiting for a specific tab only affects that tab', () => {
    toggleBrowserFullscreen('url:browser-1')

    exitBrowserFullscreen('url:browser-2')
    expect($browserFullscreenTabId.get()).toBe('url:browser-1')

    exitBrowserFullscreen('url:browser-1')
    expect($browserFullscreenTabId.get()).toBeNull()
  })

  it('exiting with no tab always leaves fullscreen', () => {
    toggleBrowserFullscreen('url:browser-1')
    exitBrowserFullscreen()

    expect($browserFullscreenTabId.get()).toBeNull()
  })
})

describe('activePreviewTabId', () => {
  it('is null with no tabs open', () => {
    expect(activePreviewTabId()).toBeNull()
  })

  // What the agent's gate keys off: the tab drive_preview would act on.
  it('is the tab the rail is showing', () => {
    openBrowserTab()

    expect(activePreviewTabId()).toBe($previewTabs.get()[0]?.id)
  })
})
