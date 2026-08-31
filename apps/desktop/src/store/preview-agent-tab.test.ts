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

import { $rightRailActiveTabId, selectRightRailTab } from './layout'
import {
  $browserSessionId,
  $dockedPreviewTabs,
  $previewTabs,
  agentPreviewTabId,
  closeRightRail,
  decodePreviewTabs,
  newBrowserTab,
  openBrowserTab,
  openPreview
} from './preview'

/** What the persisted tab list actually looks like on disk — the encoder is
 *  where ownership is dropped, so a plain JSON.stringify would not see it. */
function previewTabsStorage(): string {
  return localStorage.getItem('hermes.desktop.previewTabs.v2') ?? '[]'
}

const url = (host: string) => ({
  kind: 'url' as const,
  label: host,
  source: `https://${host}`,
  url: `https://${host}`
})


/** The runtime session ids of two conversations browsing at once. Every agent
 *  open below carries one: ownership is per RUNTIME session, so a tool-result
 *  open with no session belongs to nobody and can be taken by nobody. */
const A = 'runtime-a'
const B = 'runtime-b'

/** An agent open, from session A unless told otherwise. */
const agentOpen = (
  target: Parameters<typeof openPreview>[0],
  options: { newTab?: boolean; reveal?: boolean; sessionId?: string } = {}
) => openPreview(target, 'tool-result', { sessionId: A, ...options })

beforeEach(() => {
  closeRightRail()
  $browserSessionId.set(null)
})

describe('agent browser tabs', () => {
  it('opens its own tab instead of replacing the page you are reading', () => {
    openPreview(url('example.com'))
    agentOpen(url('docs.rs'))

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs[0]?.target.url).toBe('https://example.com')
    expect(tabs[0]?.agent).toBeFalsy()
    expect(tabs[1]?.target.url).toBe('https://docs.rs')
    expect(tabs[1]?.agent).toBe(true)
  })

  // A tab per navigation would bury the strip within one task.
  it('re-uses its own tab across a whole task', () => {
    agentOpen(url('a.com'))
    agentOpen(url('b.com'))
    agentOpen(url('c.com'))

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(1)
    expect(tabs[0]?.target.url).toBe('https://c.com')
  })

  // The dangerous case: the agent's tab exists but the person is looking at
  // their own. Resolving by "active tab" would clobber theirs.
  it('does not follow your focus onto a tab it does not own', () => {
    agentOpen(url('agent-page.com'))
    newBrowserTab()

    const mine = $previewTabs.get().at(-1)?.id

    expect(mine).toBeDefined()
    selectRightRailTab(mine!)
    agentOpen(url('agent-next.com'))

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs[0]?.target.url).toBe('https://agent-next.com')
    // Mine stayed blank — the agent went to its own tab, not the focused one.
    expect(tabs[1]?.target.url).toBe('about:blank')
  })

  it('mints a fresh tab when its own has been closed', () => {
    agentOpen(url('first.com'))
    closeRightRail()
    agentOpen(url('second.com'))

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(1)
    expect(tabs[0]?.agent).toBe(true)
    expect(tabs[0]?.target.url).toBe('https://second.com')
  })

  // Visiting is not taking over: the tab keeps answering to the agent, so the
  // agent's next step doesn't strand it and mint a duplicate.
  it('keeps ownership when you open a link in its tab', () => {
    agentOpen(url('agent.com'))
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

  // Ownership is a claim by the session that is running, not a property of the
  // tab. Persisted it would never expire: a tab the user adopted as their own
  // weeks ago would still answer to a later session's agent — the same clobber,
  // deferred. Losing it costs one extra tab and cannot cost a page.
  it('does not carry ownership across a restart', () => {
    agentOpen(url('agent.com'))

    const restored = decodePreviewTabs(previewTabsStorage())

    expect(restored).toHaveLength(1)
    expect(restored[0]?.agent).toBeFalsy()
  })

  // The user's tab, adopted long ago, comes back with no owner — so the next
  // session's agent opens beside it instead of taking it.
  it('leaves a restored tab alone and opens its own', () => {
    agentOpen(url('mine.com'))
    $previewTabs.set(decodePreviewTabs(previewTabsStorage()))

    const mine = $previewTabs.get()[0]

    expect(mine?.agent).toBeFalsy()
    selectRightRailTab(mine!.id)
    agentOpen(url('agent-next.com'))

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs[0]?.target.url).toBe('https://mine.com')
    expect(tabs[1]?.target.url).toBe('https://agent-next.com')
  })

  // Two pages side by side — comparing them, or holding a reference open.
  it('can ask for a second tab of its own', () => {
    agentOpen(url('first.com'))
    agentOpen(url('second.com'), { newTab: true })

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs.map(tab => tab.target.url)).toEqual(['https://first.com', 'https://second.com'])
    expect(tabs.every(tab => tab.agent)).toBe(true)
  })

  // Without a way back, new_tab is a one-way door: every later action goes to
  // the newest tab and the first is unreachable, since no tool selects one.
  // Re-opening a page it already holds is that way back.
  it('returns to an earlier tab by opening the page it holds', () => {
    agentOpen(url('docs.com'))
    agentOpen(url('app.com'), { newTab: true })

    agentOpen(url('docs.com'))

    expect($previewTabs.get()).toHaveLength(2)
    expect(agentPreviewTabId(A)).toBe($previewTabs.get()[0]?.id)

    // And work now lands there rather than in the newest tab.
    agentOpen(url('docs.com/page2'))

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs[0]?.target.url).toBe('https://docs.com/page2')
    expect(tabs[1]?.target.url).toBe('https://app.com')
  })

  // Having asked for a second, plain opens must land in it rather than
  // reviving the first — otherwise the agent cannot work in the tab it just
  // made.
  it('works in the newest tab it opened', () => {
    agentOpen(url('first.com'))
    agentOpen(url('second.com'), { newTab: true })
    agentOpen(url('third.com'))

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs.map(tab => tab.target.url)).toEqual(['https://first.com', 'https://third.com'])
  })

  // A file is addressed by its content, so "another tab" of it is the same tab
  // twice.
  it('ignores newTab for a file, which is addressed by its path', () => {
    const file = { kind: 'file' as const, label: 'a.ts', source: '/a.ts', url: 'file:///a.ts' }

    agentOpen(file, { newTab: true })
    agentOpen(file, { newTab: true })

    expect($previewTabs.get()).toHaveLength(1)
  })

  // A second tab must be a TAB — another row in the strip of the browser
  // already on screen — not a second pane splitting the width, and not a
  // separate window. `$dockedPreviewTabs` is what the tile strip renders
  // from; a tab missing from it has been popped out into its own window,
  // which only the pop-out button does. Nothing on the agent's path calls it.
  it('opens a tab in the same browser, not a window', () => {
    agentOpen(url('first.com'))
    agentOpen(url('second.com'), { newTab: true })

    const docked = $dockedPreviewTabs.get()

    expect(docked).toHaveLength(2)
    expect(docked.map(tab => tab.target.url)).toEqual(['https://first.com', 'https://second.com'])
    // Both are browser tabs, so the strip stacks them into the pane that is
    // already open instead of splitting a new zone off the edge (#93610).
    expect(docked.every(tab => tab.target.kind === 'url')).toBe(true)
  })

  // `openBrowserTab` is the hotkey: "show me the browser". With only the
  // agent's tab open that is the browser it should front.
  it('lets the hotkey front the agent tab rather than blanking it', () => {
    agentOpen(url('agent.com'))
    openBrowserTab()

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(1)
    expect(tabs[0]?.target.url).toBe('https://agent.com')
  })
})

/**
 * TWO CONVERSATIONS, TWO BROWSERS.
 *
 * `agentTabId` used to be one module-level variable and `browserTabId` fell
 * back to "the agent tab" for everyone, so the second chat to open a page
 * inherited the first one's tab: one browser wearing N conversations, with
 * every click, read and navigation landing on whichever page won last.
 */
describe('agent browser tabs across sessions', () => {
  it('gives each conversation its own tab', () => {
    agentOpen(url('a-side.com'))
    agentOpen(url('b-side.com'), { sessionId: B })

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs.map(tab => tab.target.url)).toEqual(['https://a-side.com', 'https://b-side.com'])
    expect(tabs.map(tab => tab.owner)).toEqual([A, B])
  })

  // THE discriminating case. A browser tab id was once derived from the url, so
  // two chats opening the same address collided by construction — an ownership
  // field alone passes the different-urls test above and still fails here.
  it('gives each conversation its own tab for the SAME url', () => {
    agentOpen(url('same.com'))
    agentOpen(url('same.com'), { sessionId: B })

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(new Set(tabs.map(tab => tab.id)).size).toBe(2)
    expect(tabs.map(tab => tab.owner)).toEqual([A, B])
  })

  // What `read_preview` resolves through. Answering from the other chat's page
  // is how an agent reads, reasons about and reports a page it never opened.
  it('resolves each conversation to its own tab', () => {
    agentOpen(url('a-side.com'))
    agentOpen(url('b-side.com'), { sessionId: B })

    const [tabA, tabB] = $previewTabs.get()

    expect(agentPreviewTabId(A)).toBe(tabA?.id)
    expect(agentPreviewTabId(B)).toBe(tabB?.id)
  })

  // Selection is global and the rail is one surface, so B's tab being fronted
  // must not re-point A.
  it('keeps each conversation on its own tab whichever one is selected', () => {
    agentOpen(url('a-side.com'))
    agentOpen(url('b-side.com'), { sessionId: B })

    const [tabA, tabB] = $previewTabs.get()

    selectRightRailTab(tabB!.id)
    expect(agentPreviewTabId(A)).toBe(tabA?.id)

    selectRightRailTab(tabA!.id)
    expect(agentPreviewTabId(B)).toBe(tabB?.id)
  })

  it('keeps each conversation navigating only its own tab', () => {
    agentOpen(url('a1.com'))
    agentOpen(url('b1.com'), { sessionId: B })

    agentOpen(url('a2.com'))
    agentOpen(url('b2.com'), { sessionId: B })

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(2)
    expect(tabs.map(tab => tab.target.url)).toEqual(['https://a2.com', 'https://b2.com'])
  })

  // A second tab for one chat is not a second tab for the other.
  it('scopes newTab to the conversation that asked', () => {
    agentOpen(url('a1.com'))
    agentOpen(url('b1.com'), { sessionId: B })
    agentOpen(url('a2.com'), { newTab: true })

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(3)
    expect(tabs.filter(tab => tab.owner === A)).toHaveLength(2)
    expect(tabs.filter(tab => tab.owner === B)).toHaveLength(1)
    // And B still works in its own, not in the newest tab on screen.
    agentOpen(url('b2.com'), { sessionId: B })
    expect($previewTabs.get()).toHaveLength(3)
    expect($previewTabs.get().find(tab => tab.owner === B)?.target.url).toBe('https://b2.com')
  })

  // Closing one chat's tab is not closing the other's.
  it('leaves the other conversation intact when one tab closes', () => {
    agentOpen(url('a-side.com'))
    agentOpen(url('b-side.com'), { sessionId: B })

    const tabA = $previewTabs.get()[0]

    $previewTabs.set($previewTabs.get().filter(tab => tab.id !== tabA!.id))

    expect($previewTabs.get()).toHaveLength(1)
    expect(agentPreviewTabId(B)).toBe($previewTabs.get()[0]?.id)
  })

  // A background turn creating its tab must not pull the rail off the page you
  // are reading. The caller decides; the store obeys.
  it('does not front a tab opened for a conversation you are not in', () => {
    agentOpen(url('mine.com'))

    const mine = $previewTabs.get()[0]

    agentOpen(url('theirs.com'), { reveal: false, sessionId: B })

    expect($previewTabs.get()).toHaveLength(2)
    expect($rightRailActiveTabId.get()).toBe(mine?.id)
  })

  // The arm that had no test and leaked: a conversation that has opened nothing
  // of its own falls back to "the page you are looking at" — but another chat's
  // owned tab is not a page it is looking at. Unfiltered, an agent's very first
  // `read_preview` answered from whichever tab was active.
  it('gives a conversation that has opened nothing no claim on another\'s tab', () => {
    agentOpen(url('a-side.com'))

    const tabA = $previewTabs.get()[0]

    selectRightRailTab(tabA!.id)

    expect(agentPreviewTabId(B)).toBeNull()
  })

  // But a page the PERSON opened is still everyone's, which is the whole point
  // of the fallback and must survive the fix above.
  it('still falls back to a page you opened yourself', () => {
    openPreview(url('mine.com'))

    expect(agentPreviewTabId(B)).toBe($previewTabs.get()[0]?.id)
  })

  // The strip's "+" joins the browser you are LOOKING at, which belongs to a
  // conversation — so the tab does too, rather than becoming a stray everyone
  // sees. Deliberate change: it used to make an unowned tab.
  it('gives a new tab to the conversation whose browser is open', () => {
    $browserSessionId.set(A)
    newBrowserTab()

    expect($previewTabs.get().at(-1)?.owner).toBe(A)
  })

  // A chat that has never browsed gets a browser of its own — not the other
  // chat's page, and not that page navigated to about:blank.
  it('opens a blank browser owned by the conversation that asked', () => {
    agentOpen(url('a-side.com'))

    openBrowserTab(B)

    const mine = $previewTabs.get().filter(tab => tab.owner === B)

    expect(mine).toHaveLength(1)
    expect(mine[0]?.target.url).toBe('about:blank')
    // A's page is untouched — this is the clobber the direct-select fixed.
    expect($previewTabs.get().find(tab => tab.owner === A)?.target.url).toBe('https://a-side.com')
  })

  it('fronts a conversation own browser and points the rail at it', () => {
    agentOpen(url('a-side.com'))
    agentOpen(url('b-side.com'), { sessionId: B })
    $browserSessionId.set(A)

    openBrowserTab(B)

    expect($browserSessionId.get()).toBe(B)
    expect($rightRailActiveTabId.get()).toBe($previewTabs.get().find(tab => tab.owner === B)?.id)
  })
})
