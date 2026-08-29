/**
 * The agent browses BESIDE you, not over you.
 *
 * `open_preview` used to resolve to "the browser tab you're looking at", which
 * is right for a link you clicked and wrong for a tool call — the agent's next
 * navigation silently replaced the page the person was reading (#93190). These
 * cover the ownership rule that fixes it, from both directions: the agent never
 * takes a tab that isn't its own, and a person's own opens still behave exactly
 * as they did.
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { selectRightRailTab } from './layout'
import {
  $previewTabs,
  closeRightRail,
  decodePreviewTabs,
  newBrowserTab,
  openBrowserTab,
  openPreview
} from './preview'

const url = (host: string) => ({
  kind: 'url' as const,
  label: host,
  source: `https://${host}`,
  url: `https://${host}`
})

beforeEach(() => {
  closeRightRail()
})

describe('agent browser tabs', () => {
  it('opens its own tab instead of replacing the page you are reading', () => {
    openPreview(url('example.com'))
    openPreview(url('docs.rs'), 'tool-result')

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs[0]?.target.url).toBe('https://example.com')
    expect(tabs[0]?.agent).toBeFalsy()
    expect(tabs[1]?.target.url).toBe('https://docs.rs')
    expect(tabs[1]?.agent).toBe(true)
  })

  // A tab per navigation would bury the strip within one task.
  it('re-uses its own tab across a whole task', () => {
    openPreview(url('a.com'), 'tool-result')
    openPreview(url('b.com'), 'tool-result')
    openPreview(url('c.com'), 'tool-result')

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(1)
    expect(tabs[0]?.target.url).toBe('https://c.com')
  })

  // The dangerous case: the agent's tab exists but the person is looking at
  // their own. Resolving by "active tab" would clobber theirs.
  it('does not follow your focus onto a tab it does not own', () => {
    openPreview(url('agent-page.com'), 'tool-result')
    newBrowserTab()

    const mine = $previewTabs.get().at(-1)?.id

    expect(mine).toBeDefined()
    selectRightRailTab(mine!)
    openPreview(url('agent-next.com'), 'tool-result')

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs[0]?.target.url).toBe('https://agent-next.com')
    // Mine stayed blank — the agent went to its own tab, not the focused one.
    expect(tabs[1]?.target.url).toBe('about:blank')
  })

  it('mints a fresh tab when its own has been closed', () => {
    openPreview(url('first.com'), 'tool-result')
    closeRightRail()
    openPreview(url('second.com'), 'tool-result')

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(1)
    expect(tabs[0]?.agent).toBe(true)
    expect(tabs[0]?.target.url).toBe('https://second.com')
  })

  // Visiting is not taking over: the tab keeps answering to the agent, so the
  // agent's next step doesn't strand it and mint a duplicate.
  it('keeps ownership when you open a link in its tab', () => {
    openPreview(url('agent.com'), 'tool-result')
    openPreview(url('link.com'), 'explicit-link')

    expect($previewTabs.get()).toHaveLength(1)
    expect($previewTabs.get()[0]?.agent).toBe(true)
  })

  it('leaves your own browsing untouched', () => {
    openPreview(url('one.com'))
    openPreview(url('two.com'))

    const tabs = $previewTabs.get()

    // Still the one browser, navigated — a link does not stack a tab.
    expect(tabs).toHaveLength(1)
    expect(tabs[0]?.target.url).toBe('https://two.com')
    expect(tabs[0]?.agent).toBeFalsy()
  })

  // Ownership is persisted, and the restore path validates tabs field by
  // field. If the flag were dropped on the way back in, the agent would come
  // up after a restart believing it owned nothing — and take the first browser
  // tab it found, which is the original bug wearing a hat.
  it('still owns its tab after a restart', () => {
    openPreview(url('agent.com'), 'tool-result')
    newBrowserTab()

    const saved = JSON.stringify($previewTabs.get())
    const restored = decodePreviewTabs(saved)

    expect(restored).toHaveLength(2)
    expect(restored[0]?.agent).toBe(true)
    expect(restored[1]?.agent).toBeFalsy()

    // And the rule still holds against the restored list.
    $previewTabs.set(restored)
    selectRightRailTab(restored[1]!.id)
    openPreview(url('after-restart.com'), 'tool-result')

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs[0]?.target.url).toBe('https://after-restart.com')
    expect(tabs[1]?.target.url).toBe('about:blank')
  })

  // `openBrowserTab` is the hotkey: "show me the browser". With only the
  // agent's tab open that is the browser it should front.
  it('lets the hotkey front the agent tab rather than blanking it', () => {
    openPreview(url('agent.com'), 'tool-result')
    openBrowserTab()

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(1)
    expect(tabs[0]?.target.url).toBe('https://agent.com')
  })
})
