/**
 * THE WHOLE TRIP, one tool call after another.
 *
 * Every other test here covers one seam. This one plays the session the user
 * actually described — open a page, read it, navigate, click, come back — and
 * asserts the thing no single-seam test can: that the tools agree with each
 * other about WHICH PAGE they are talking about.
 *
 * That agreement is where this feature broke twice. `open_preview` resolved to
 * the tab you were looking at (#93190), then `drive_preview` did, then
 * `read_preview` still did after the others had been fixed — so the agent could
 * click one page and read another with nothing to show for it. The checks below
 * would all pass individually with that bug present; only run together do they
 * catch it.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { selectRightRailTab } from '@/store/layout'
import { $previewTabs, closeRightRail, newBrowserTab, openPreview } from '@/store/preview'

import { previewConsoleState } from './preview-console-store'
import { registerPreviewNav } from './preview-nav'
import { readActivePreview } from './preview-reader'

/** A stand-in for one live browser tab: the page it shows, what its console has
 *  said, and the navigation the pane would perform. */
function mountTab(tabId: string, page: { text: string; title: string; url: string }) {
  const nav = { back: vi.fn(), forward: vi.fn(), navigate: vi.fn(), reload: vi.fn() }
  const dispose = registerPreviewNav(tabId, nav)

  return { dispose, nav, page }
}

const target = (host: string) => ({
  kind: 'url' as const,
  label: host,
  source: `https://${host}`,
  url: `https://${host}`
})

beforeEach(() => {
  closeRightRail()
})

describe('a whole browsing turn', () => {
  // The session the user asked for: they are reading something, the agent is
  // sent to do its own work, and neither disturbs the other.
  it('never touches the page the user is reading', async () => {
    // The user has their own page open and focused.
    openPreview(target('news.example.com'))

    const mine = $previewTabs.get()[0]!.id

    // The agent is asked to go somewhere. It must not land in `mine`.
    openPreview(target('docs.example.com'), 'tool-result')

    const tabs = $previewTabs.get()
    const agentTab = tabs.find(tab => tab.agent)

    expect(tabs).toHaveLength(2)
    expect(agentTab).toBeDefined()
    expect(agentTab!.id).not.toBe(mine)
    expect(tabs.find(tab => tab.id === mine)!.target.url).toBe('https://news.example.com')

    // The user clicks back to their own tab WHILE the agent works — the exact
    // moment the read/act split used to diverge.
    selectRightRailTab(mine)

    const nav = mountTab(agentTab!.id, {
      text: 'Docs body',
      title: 'Docs',
      url: 'https://docs.example.com'
    })

    // The agent navigates. It must reach ITS tab, not the focused one.
    const { actOnActivePreview } = await import('./preview-act')
    const result = await actOnActivePreview({ kind: 'navigate', url: 'https://docs.example.com/api' })

    expect(result.success).toBe(true)
    expect(nav.nav.navigate).toHaveBeenCalledWith('https://docs.example.com/api')

    nav.dispose()
  })

  // The read half of the same agreement. A reader is registered for BOTH tabs,
  // so a read that followed focus would silently answer from the wrong one.
  it('reads the page it is driving, not the one you clicked', async () => {
    openPreview(target('mine.example.com'))

    const mine = $previewTabs.get()[0]!.id

    openPreview(target('agent.example.com'), 'tool-result')

    const agentTabId = $previewTabs.get().find(tab => tab.agent)!.id
    const { registerPreviewPageReader } = await import('./preview-reader')

    const disposeMine = registerPreviewPageReader(mine, async () => ({
      text: 'THE USER PAGE',
      title: 'Mine',
      url: 'https://mine.example.com'
    }))

    const disposeAgent = registerPreviewPageReader(agentTabId, async () => ({
      text: 'THE AGENT PAGE',
      title: 'Agent',
      url: 'https://agent.example.com'
    }))

    // Focus sits on the user's tab.
    selectRightRailTab(mine)

    const read = await readActivePreview()

    expect(read?.text).toBe('THE AGENT PAGE')
    expect(read?.url).toBe('https://agent.example.com')

    disposeMine()
    disposeAgent()
  })

  // The errors the whole console half exists for, arriving the way they do in
  // life: the agent acts, and the count rides back on the result it already
  // asked for — no extra call, and warnings included.
  it('learns a page started failing without asking', async () => {
    openPreview(target('app.example.com'), 'tool-result')

    const tabId = $previewTabs.get()[0]!.id
    const nav = mountTab(tabId, { text: '', title: 'App', url: 'https://app.example.com' })
    const { actOnActivePreview } = await import('./preview-act')

    // A quiet page says nothing — a breadcrumb on every result would be noise.
    const quiet = await actOnActivePreview({ kind: 'reload' })

    expect(quiet.console_since_last_call).toBeUndefined()

    // The page starts complaining. The translation warning is the case that
    // an errors-only counter would have missed entirely.
    previewConsoleState(tabId).append({ level: 2, message: 'missing translation for key "checkout.title"' })
    previewConsoleState(tabId).append({ level: 3, message: 'TypeError: undefined is not a function' })

    const noisy = await actOnActivePreview({ kind: 'reload' })

    expect(noisy.console_since_last_call).toEqual({ errors: 1, warnings: 1 })

    // Told once, not on every result afterwards.
    const after = await actOnActivePreview({ kind: 'reload' })

    expect(after.console_since_last_call).toBeUndefined()

    nav.dispose()
  })

  // Two pages side by side, then back to the first — the arrangement `new_tab`
  // exists for, which was unusable while the newest tab won every lookup.
  it('works across two of its own tabs and back', async () => {
    openPreview(target('docs.example.com'), 'tool-result')
    openPreview(target('app.example.com'), 'tool-result', { newTab: true })

    const [docs, app] = $previewTabs.get()
    const docsNav = mountTab(docs!.id, { text: '', title: 'Docs', url: 'https://docs.example.com' })
    const appNav = mountTab(app!.id, { text: '', title: 'App', url: 'https://app.example.com' })
    const { actOnActivePreview } = await import('./preview-act')

    // Work lands in the tab it just opened.
    await actOnActivePreview({ kind: 'navigate', url: 'https://app.example.com/login' })

    expect(appNav.nav.navigate).toHaveBeenCalledWith('https://app.example.com/login')
    expect(docsNav.nav.navigate).not.toHaveBeenCalled()

    // Re-opening the page the other tab holds is the way back.
    openPreview(target('docs.example.com'), 'tool-result')
    await actOnActivePreview({ kind: 'navigate', url: 'https://docs.example.com/auth' })

    expect(docsNav.nav.navigate).toHaveBeenCalledWith('https://docs.example.com/auth')
    // Still two tabs — returning is not opening a third.
    expect($previewTabs.get()).toHaveLength(2)

    docsNav.dispose()
    appNav.dispose()
  })

  // With no tab of its own, the agent is talking about the page on screen —
  // the behaviour that was right before it had one, and still is.
  it('falls back to your page when it has no tab', async () => {
    newBrowserTab()
    openPreview(target('only.example.com'))

    const tabId = $previewTabs.get().find(tab => tab.target.kind === 'url')!.id
    const { registerPreviewPageReader } = await import('./preview-reader')

    const dispose = registerPreviewPageReader(tabId, async () => ({
      text: 'ON SCREEN',
      title: 'Only',
      url: 'https://only.example.com'
    }))

    expect((await readActivePreview())?.text).toBe('ON SCREEN')

    dispose()
  })

  // Two different "you cannot navigate this" cases, and the agent is told
  // which. A file peek registers no pane at all; a remote HTML preview
  // registers one that has history but no address to go to.
  it('distinguishes nothing-open from a pane with no address', async () => {
    openPreview(
      { kind: 'file', label: 'notes.md', source: '/notes.md', url: 'file:///notes.md' },
      'tool-result'
    )

    const { actOnActivePreview } = await import('./preview-act')
    const nothing = await actOnActivePreview({ kind: 'navigate', url: 'https://example.com' })

    expect(nothing.success).toBe(false)
    expect(nothing.error).toMatch(/no live page is open/i)

    // Now a pane that exists but cannot take an address — `navigate` is
    // optional on the handle precisely for this.
    closeRightRail()
    openPreview(target('remote.example.com'), 'tool-result')

    const tabId = $previewTabs.get()[0]!.id
    const dispose = registerPreviewNav(tabId, { back: vi.fn(), forward: vi.fn(), reload: vi.fn() })
    const addressless = await actOnActivePreview({ kind: 'navigate', url: 'https://example.com' })

    expect(addressless.success).toBe(false)
    expect(addressless.error).toMatch(/not a browser/i)

    // History still works there — it is the ADDRESS that is missing.
    expect((await actOnActivePreview({ kind: 'reload' })).success).toBe(true)

    dispose()
  })

  // A url with nothing in it is a mistake worth naming before the bridge.
  it('refuses a navigate with no address', async () => {
    openPreview(target('app.example.com'), 'tool-result')

    const tabId = $previewTabs.get()[0]!.id
    const nav = mountTab(tabId, { text: '', title: 'App', url: 'https://app.example.com' })
    const { actOnActivePreview } = await import('./preview-act')
    const result = await actOnActivePreview({ kind: 'navigate', url: '   ' })

    expect(result.success).toBe(false)
    expect(result.error).toMatch(/needs a url/i)
    expect(nav.nav.navigate).not.toHaveBeenCalled()

    nav.dispose()
  })
})
