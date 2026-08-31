/**
 * ONE BROWSER PER CONVERSATION.
 *
 * The rail was one strip holding every chat's pages, so three projects' tabs
 * piled into one row. Each conversation now shows its own browser.
 *
 * HIDDEN, NOT FILTERED, and that is the whole design constraint. The pane
 * mirror registers from `$dockedPreviewTabs`; dropping a tab out of that list
 * calls `removeTreePane`, which destroys the pane and the live page inside it —
 * so scoping the list would tear down whatever the OTHER conversation's agent
 * was in the middle of driving and reload it on the way back, losing its
 * scroll, its form and its login. Hiding leaves every pane registered and
 * mounted; it only stops the strip from drawing the tab.
 *
 * The second constraint is a scar: filtering this mirror by workspace mode once
 * dropped the pane out of Bot Mode entirely, so `openPreview` ran and a clicked
 * link looked like a no-op. Anything a PERSON opened is unowned and stays
 * visible from everywhere.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $hiddenTreePanes } from '@/components/pane-shell/tree/store'
import { $browserSessionId, $dockedPreviewTabs, $previewTabs, closeRightRail, openPreview } from '@/store/preview'

const A = 'runtime-a'
const B = 'runtime-b'

/** Which conversations still have a surface on screen. The orphan rule below
 *  turns on exactly this. */
const onScreen = vi.hoisted(() => ({ ids: new Set<string>() }))

const storedFocus = vi.hoisted(() => ({ id: null as null | string }))

vi.mock('@/store/session-states', () => ({
  $focusedStoredSessionId: { get: () => storedFocus.id },
  runtimeHasOpenSurface: (id: null | string) => Boolean(id && onScreen.ids.has(id))
}))

const { syncBrowserSessionPanes } = await import('./preview-tile')

const url = (host: string) => ({
  kind: 'url' as const,
  label: host,
  source: `https://${host}`,
  url: `https://${host}`
})

const agentOpen = (host: string, sessionId: string) => openPreview(url(host), 'tool-result', { sessionId })

/** The urls the strip would actually draw. */
function shown(): string[] {
  syncBrowserSessionPanes()

  const hidden = $hiddenTreePanes.get()

  return $dockedPreviewTabs
    .get()
    .filter(tab => !hidden.has(`preview-tile:${tab.id}`))
    .map(tab => tab.target.url)
}

beforeEach(() => {
  closeRightRail()
  $hiddenTreePanes.set(new Set())
  $browserSessionId.set(null)
  onScreen.ids = new Set([A, B])
  storedFocus.id = null
})

describe('per-conversation browser', () => {
  it('shows only the tabs of the conversation you are in', () => {
    agentOpen('a-side.com', A)
    agentOpen('b-side.com', B)

    $browserSessionId.set(A)
    expect(shown()).toEqual(['https://a-side.com'])

    $browserSessionId.set(B)
    expect(shown()).toEqual(['https://b-side.com'])
  })

  // Every pane stays REGISTERED — only its tab is hidden. This is the assertion
  // that stands between this feature and destroying a page mid-task.
  it('keeps the other conversation page mounted rather than removing it', () => {
    agentOpen('a-side.com', A)
    agentOpen('b-side.com', B)
    $browserSessionId.set(A)
    syncBrowserSessionPanes()

    expect($dockedPreviewTabs.get()).toHaveLength(2)
    expect($hiddenTreePanes.get().size).toBe(1)
  })

  // THE regression. A link, a file, an artifact — anything a person opened
  // belongs to no conversation and has to stay reachable from all of them, or
  // clicking a link in a chat that owns no browser does nothing at all.
  it('keeps everything a person opened visible from every conversation', () => {
    openPreview(url('link-i-clicked.com'), 'explicit-link')
    agentOpen('agent-page.com', A)

    $browserSessionId.set(A)
    expect(shown()).toContain('https://link-i-clicked.com')

    $browserSessionId.set(B)
    expect(shown()).toEqual(['https://link-i-clicked.com'])
  })

  it('shows a file the file tree opened from every conversation', () => {
    openPreview({ kind: 'file', label: 'a.ts', source: '/a.ts', url: 'file:///a.ts' }, 'file-browser')
    agentOpen('agent-page.com', A)

    $browserSessionId.set(B)
    expect(shown()).toEqual(['file:///a.ts'])
  })

  // Before any chat is focused — first paint, a window with nothing selected —
  // an empty rail is a worse answer than an unscoped one. This is the state the
  // Bot Mode regression actually lived in.
  it('shows everything while no conversation is established', () => {
    agentOpen('a-side.com', A)
    agentOpen('b-side.com', B)

    expect($browserSessionId.get()).toBeNull()
    expect(shown()).toEqual(['https://a-side.com', 'https://b-side.com'])
  })

  // A tab owned by a conversation that has ended is scoped to a runtime id that
  // will never be current again. Left hidden it would be invisible from every
  // conversation and unreachable until a restart — "my tab vanished".
  it('shows an orphaned tab once its conversation is gone', () => {
    agentOpen('a-side.com', A)
    agentOpen('b-side.com', B)

    $browserSessionId.set(B)
    expect(shown()).toEqual(['https://b-side.com'])

    onScreen.ids = new Set([B])
    expect(shown()).toEqual(['https://a-side.com', 'https://b-side.com'])
  })

  // Re-hiding must be reversible: coming back to a conversation brings its tabs
  // back rather than leaving a one-way hide behind.
  it('brings a conversation tabs back when you return to it', () => {
    agentOpen('a-side.com', A)
    agentOpen('b-side.com', B)

    $browserSessionId.set(B)
    expect(shown()).toEqual(['https://b-side.com'])

    $browserSessionId.set(A)
    expect(shown()).toEqual(['https://a-side.com'])
  })

  // THE restart hole the user found: `owner` is a runtime id and is dropped on
  // the way out, so a restored tab used to come back belonging to nobody and
  // showed in EVERY conversation — reopen the app and the other chat's page is
  // sitting in yours. `ownerKey` is the half that survives.
  it('keeps a restored tab with its own conversation across a restart', () => {
    agentOpen('a-side.com', A)
    // What restore produces: the runtime half gone, the stored half kept.
    $previewTabs.set($previewTabs.get().map(tab => ({ ...tab, agent: true, owner: undefined, ownerKey: 'stored-a' })))

    $browserSessionId.set(B)
    storedFocus.id = 'stored-b'
    expect(shown()).toEqual([])

    $browserSessionId.set(A)
    storedFocus.id = 'stored-a'
    expect(shown()).toEqual(['https://a-side.com'])
  })
})
