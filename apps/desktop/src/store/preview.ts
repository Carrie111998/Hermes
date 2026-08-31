import { atom, computed } from 'nanostores'

import { persistentAtom } from '@/lib/persisted'
import { readKey } from '@/lib/storage'
import { normalize } from '@/lib/text'

import { $rightRailActiveTabId, type RightRailTabId, selectRightRailTab } from './layout'
import { canOpenBrowserWindow, openBrowserInNewWindow } from './windows'

/**
 * PREVIEW RAIL — one list of tabs, one way in.
 *
 * Everything the rail can show is a `PreviewTarget` in `$previewTabs`: a file
 * on disk, a live URL, or a generated artifact. There is no privileged "live
 * preview" slot alongside the tabs; `openPreview` is the only entry point, so
 * a tool result, a file-browser click, and an artifact card all travel the
 * same road and behave identically once open.
 *
 * Tabs are global and outlive the session that created them, like tabs
 * anywhere else — they close when you close them.
 */

export interface PreviewTarget {
  binary?: boolean
  byteSize?: number
  /** Inline image bytes (a `data:` URL) when the renderer already holds them —
   * e.g. a pasted/dropped screenshot whose only on-disk copy is a transient
   * path the preview can't reliably re-read. Rendered directly and NOT
   * persisted (it would bloat localStorage). */
  dataUrl?: string
  /** `artifact` targets have nothing behind them on disk or on the network —
   * `url` is an id into the artifact registry, which owns the content. They
   * are what lets the rail preview generated HTML the workspace never saw. */
  kind: 'artifact' | 'file' | 'url'
  label: string
  large?: boolean
  language?: string
  mimeType?: string
  path?: string
  previewKind?: 'binary' | 'html' | 'image' | 'pdf' | 'text'
  renderMode?: 'preview' | 'source'
  source: string
  /** Runtime-only target that cannot be restored from persisted state. */
  transient?: boolean
  url: string
}

export interface PreviewServerRestart {
  message?: string
  status: 'complete' | 'error' | 'running'
  taskId: string
  url: string
}

/** Where an open came from. Only affects how an HTML file is first rendered:
 *  browsing files is "peek at the source", a tool/link handing you something is
 *  "run it". Not a separate code path — just a property of the target. */
export type PreviewRecordSource = 'explicit-link' | 'file-browser' | 'manual' | 'tool-result'

export interface PreviewTab {
  /** Opened by the agent, and so the tab it browses in. The agent re-uses this
   *  tab across a whole task instead of stacking one per navigation, and never
   *  reaches for a tab without it — see `browserTabId`. */
  agent?: boolean
  id: RightRailTabId
  /** RUNTIME session id of the agent that opened this tab, when one did.
   *
   *  Runtime, never the stored id. A session-keyed preview registry existed
   *  once and was removed in 96999b116 because it was keyed on the STORED id,
   *  which lands late: an `open_preview` from a session whose stored id had not
   *  arrived was written and then immediately reconciled away, so the pane
   *  flashed and vanished. The runtime id is in hand at the moment the tool
   *  runs. This is also why ownership is a field on the one tab list rather
   *  than a second registry beside it — two lists under two id rules is the
   *  shape that failed.
   *
   *  Absent = nobody's: a file-browser click, an artifact, a link you opened.
   *  Those stay visible to everyone. */
  owner?: string
  /** STORED session id of the conversation that owns this tab — the same claim
   *  as `owner`, in the one form that survives a restart.
   *
   *  `owner` is a runtime id and is dropped on the way out, so without this
   *  every restored tab came back belonging to nobody and showed in EVERY
   *  conversation: reopen the app and the other chat's page is sitting in
   *  yours again.
   *
   *  Safe where the deleted registry was not, because this id decides only what
   *  the strip DRAWS — nothing routes a write through it. The registry removed
   *  in 96999b116 keyed the WRITE path on the stored id and lost races against
   *  its own arrival; a late id here can only leave a tab visible a moment
   *  longer. */
  ownerKey?: string
  target: PreviewTarget
}

const TABS_STORAGE_KEY = 'hermes.desktop.previewTabs.v2'
/** Superseded by the tab list above; cleared so it can't leak forever. */
const LEGACY_SESSION_REGISTRY_KEY = 'hermes.desktop.sessionPreviews.v1'

function isPreviewTarget(value: unknown): value is PreviewTarget {
  if (!value || typeof value !== 'object') {
    return false
  }

  const r = value as Record<string, unknown>

  return (
    (r.kind === 'artifact' || r.kind === 'file' || r.kind === 'url') &&
    typeof r.label === 'string' &&
    typeof r.source === 'string' &&
    typeof r.url === 'string'
  )
}

// Artifact tabs are never written (their registry is memory-only), so a
// restored artifact row is stale storage — drop it rather than reviving a tab
// with nothing behind it.
function isPreviewTab(value: unknown): value is PreviewTab {
  if (!value || typeof value !== 'object') {
    return false
  }

  const r = value as Record<string, unknown>

  return typeof r.id === 'string' && (r.id.startsWith('file:') || r.id.startsWith('url:')) && isPreviewTarget(r.target)
}

function isPdfFileTarget(target: PreviewTarget): boolean {
  if (target.kind !== 'file') {
    return false
  }

  if (target.mimeType?.toLowerCase() === 'application/pdf') {
    return true
  }

  if ([target.path, target.source].some(value => (value ? /\.pdf$/i.test(value) : false))) {
    return true
  }

  try {
    return /\.pdf$/i.test(new URL(target.url).pathname)
  } catch {
    return false
  }
}

/** Upgrade tabs persisted by builds that classified PDFs as generic binary.
 * Without this restore-time migration, an already-open PDF keeps taking the
 * obsolete raw-binary path after Desktop itself has been upgraded. */
export function decodePreviewTabs(raw: string): PreviewTab[] {
  const parsed = JSON.parse(raw) as unknown

  return (Array.isArray(parsed) ? parsed.filter(isPreviewTab) : []).map(tab =>
    isPdfFileTarget(tab.target) && tab.target.previewKind === 'binary'
      ? { ...tab, target: { ...tab.target, previewKind: 'pdf' as const } }
      : tab
  )
}

export const $previewTabs = persistentAtom<PreviewTab[]>(TABS_STORAGE_KEY, [], {
  decode: decodePreviewTabs,
  // Inline bytes are not restorable. Strip them from images, and skip remote
  // HTML and artifact tabs that cannot render without their in-memory payload.
  // `agent` is dropped on the way out. Ownership is a claim by the session that
  // is running, not a property of the tab: persisted, it never expires, and a
  // tab the user adopted as their own weeks ago would still answer to the next
  // session's agent — the #93190 clobber, deferred rather than removed. Losing
  // it across a restart costs one extra tab and cannot cost a page.
  encode: tabs =>
    JSON.stringify(
      tabs.filter(
        tab =>
          tab.target.kind !== 'artifact' &&
          !tab.target.transient &&
          !(tab.target.previewKind === 'html' && tab.target.dataUrl)
      ),
      // `owner` goes out with `agent`, and for the same reason plus one: it
      // names a RUNTIME session, and no runtime survives the restart. A
      // restored owner would bind the tab to a dead id — invisible to every
      // live session and reachable by none.
      // `ownerKey` deliberately survives — it is the restart-durable half.
      (key, value) => (key === 'agent' || key === 'dataUrl' || key === 'owner' ? undefined : value)
    )
})

if (typeof window !== 'undefined') {
  try {
    window.localStorage.removeItem(LEGACY_SESSION_REGISTRY_KEY)
  } catch {
    // Storage access can throw in locked-down contexts; nothing depends on it.
  }
}

/** The tab the rail actually shows. A stale or missing selection falls back to
 *  the first tab, so the strip, `⌘W`, and the pane never disagree about which
 *  tab is on screen. */
function resolveActiveTab(tabs: PreviewTab[], activeTabId: RightRailTabId | null): PreviewTab | null {
  return tabs.find(tab => tab.id === activeTabId) ?? tabs[0] ?? null
}

function activePreviewTab(): PreviewTab | null {
  return resolveActiveTab($previewTabs.get(), $rightRailActiveTabId.get())
}

/** Which of its own tabs each RUNTIME session's agent is working in. Module
 *  state, not persisted: it is a property of the turn in flight, and a restored
 *  one would point the agent at a tab it has no memory of opening.
 *
 *  Keyed, because it used to be one variable for the whole app. Two chats
 *  browsing at once both resolved to it, so the second `open_preview` landed in
 *  the first one's tab and every later click, read and navigation went to
 *  whichever page won last — one shared browser wearing N conversations. */
const agentTabBySession = new Map<string, RightRailTabId>()

/** Key for agent opens that arrive with no session id. */
const UNSCOPED = '\u0000unscoped'

/** Is `tab` the browsing tab of the agent in `sessionId`?
 *
 *  One predicate for both lookups below — they must agree, or "go back to the
 *  tab already holding this page" resolves to a different tab than "keep
 *  working where I was", and `new_tab` becomes one-way again.
 *
 *  A null session matches the agent tabs nobody has claimed rather than
 *  nothing: an event that arrives unscoped must still browse beside the user,
 *  never fall through to the page they are reading. */
function agentOwns(tab: PreviewTab, sessionId: null | string): boolean {
  return Boolean(isBrowserTab(tab) && tab.agent && (sessionId ? tab.owner === sessionId : !tab.owner))
}

/** The tab an AGENT ACTION operates on — clicking, typing, navigating, reading.
 *
 *  Not the active tab. Resolving a write by "what's on screen" means the agent
 *  clicks around the page you just switched to, which is the same mistake as
 *  #93190 one layer down: the agent's tab is where it opened its page, and
 *  focus is yours to move while it works.
 *
 *  And not ANY agent tab: `sessionId` is which conversation is asking. Without
 *  it, session A's `read_preview` answers from session B's page.
 *
 *  Falls back to the active tab only when this session has no tab of its own —
 *  "click the button on this page" about the page you are looking at. READS
 *  resolve through here too: `read_preview` answering from a different tab than
 *  `drive_preview` acted on let the agent click one page and report another. */
export function agentPreviewTabId(sessionId: null | string): RightRailTabId | null {
  const tabs = $previewTabs.get()

  // The fallback is "the page you are looking at", for a conversation that has
  // opened nothing of its own — but only among pages it is ALLOWED to look at.
  // Unfiltered, a session that had never called `open_preview` fell through to
  // whatever was active, which can be ANOTHER conversation's owned tab: it
  // would then read, reason about and report a page it never opened, one chat
  // over. That is the failure `readActivePreview`'s own header forbids.
  const visible = tabs.filter(tab => previewTabBelongsToSession(tab, sessionId))

  return (agentTab(tabs, sessionId) ?? resolveActiveTab(visible, $rightRailActiveTabId.get()))?.id ?? null
}

/** This session's CURRENT tab, or the newest it owns if that one has been
 *  closed.
 *
 *  Tracked rather than derived: resolving by "the newest agent tab" alone made
 *  `new_tab` one-way — once the agent opened a second tab, every later action
 *  went there and the first was unreachable for the rest of the session, since
 *  no tool selects a browser tab. */
function agentTab(tabs: PreviewTab[], sessionId: null | string): PreviewTab | undefined {
  const owned = (tab: PreviewTab) => agentOwns(tab, sessionId)
  const current = tabs.find(tab => tab.id === agentTabBySession.get(sessionId ?? UNSCOPED) && owned(tab))

  return current ?? tabs.findLast(owned)
}

// A restored active id whose tab didn't survive validation would leave the rail
// pointing at nothing.
selectRightRailTab(activePreviewTab()?.id ?? null)

/** The target the rail is currently showing, or null when it has no tabs. */
export const $previewTarget = computed(
  [$previewTabs, $rightRailActiveTabId],
  (tabs, activeTabId) => resolveActiveTab(tabs, activeTabId)?.target ?? null
)

/** Raw `source` strings of every open tab, for the composer rows that toggle a
 *  preview open and closed by the target they were handed. */
export const $previewTabSources = computed($previewTabs, tabs => tabs.map(tab => tab.target.source))

export interface BrowserPage {
  title: string
  url: string
}

/**
 * What each Browser tab is SHOWING right now, as opposed to the target it was
 * opened with. Kept out of the target on purpose: the pane builds its guest
 * from `target.url`, so folding navigation back in would tear the webview down
 * and lose the history behind it. Memory-only — a restored tab reports again
 * on its first load.
 */
export const $browserPages = atom<Record<string, BrowserPage>>({})

export function noteBrowserPage(tabId: string, page: BrowserPage) {
  const current = $browserPages.get()[tabId]

  if (current?.title === page.title && current.url === page.url) {
    return
  }

  $browserPages.set({ ...$browserPages.get(), [tabId]: page })
}

export function forgetBrowserPage(tabId: string) {
  const { [tabId]: gone, ...rest } = $browserPages.get()

  if (gone) {
    $browserPages.set(rest)
  }
}

/** Write the page a Browser is showing back onto its persisted tab. The
 *  webview is built from `target.url`, so this is for hand-off (pop-out /
 *  dock-back), not for every in-page hop — that would tear the guest down. */
export function commitBrowserTabLocation(tabId: string, url: string, title?: string) {
  const nextUrl = url.trim()

  if (!tabId || !nextUrl) {
    return
  }

  const tabs = $previewTabs.get()
  const index = tabs.findIndex(tab => tab.id === tabId)

  if (index === -1) {
    return
  }

  const tab = tabs[index]
  const nextTitle = title?.trim()

  if (tab.target.kind !== 'url' || (tab.target.url === nextUrl && (!nextTitle || tab.target.label === nextTitle))) {
    return
  }

  $previewTabs.set(
    tabs.map((item, i) =>
      i === index
        ? {
            ...item,
            target: {
              ...item.target,
              ...(nextTitle ? { label: nextTitle } : {}),
              url: nextUrl
            }
          }
        : item
    )
  )
}

/** Pull one tab from storage into this renderer's atom. A sibling window
 *  (the pop-out) may have committed a newer URL that we never saw. */
export function adoptPersistedBrowserTab(tabId: string) {
  if (!tabId) {
    return
  }

  try {
    const raw = readKey(TABS_STORAGE_KEY)

    if (!raw) {
      return
    }

    const persisted = decodePreviewTabs(raw).find(tab => tab.id === tabId)

    if (!persisted || persisted.target.kind !== 'url') {
      return
    }

    commitBrowserTabLocation(tabId, persisted.target.url, persisted.target.label)
  } catch {
    // Storage can throw; the in-memory tab stays as it was.
  }
}

/** Pop the in-app Browser into its own OS window. Shared by the address-bar
 *  glyph and the tab context menu so they cannot drift. */
export function popOutBrowserTab(tabId: string) {
  if (!tabId || !canOpenBrowserWindow()) {
    return
  }

  const tab = $previewTabs.get().find(item => item.id === tabId)

  if (!tab || tab.target.kind !== 'url') {
    return
  }

  const page = $browserPages.get()[tabId]

  markBrowserTabPopped(tabId, true)
  commitBrowserTabLocation(tabId, page?.url || tab.target.url, page?.title)
  void openBrowserInNewWindow(tabId).then(ok => {
    if (!ok) {
      markBrowserTabPopped(tabId, false)
    }
  })
}

/** Tabs currently shown in a popped-out Browser window. The docked tree
 *  hides them so the page isn't in two places; closing the window docks
 *  them again. Memory-only — a relaunch with no pop-out window restores. */
export const $poppedBrowserTabIds = atom<ReadonlySet<string>>(new Set())

export function markBrowserTabPopped(tabId: string, popped: boolean) {
  const current = $poppedBrowserTabIds.get()

  if (current.has(tabId) === popped) {
    return
  }

  const next = new Set(current)

  if (popped) {
    next.add(tabId)
  } else {
    next.delete(tabId)
  }

  $poppedBrowserTabIds.set(next)
}

/** WHOSE browser the rail is showing — the conversation the tabs belong to.
 *
 *  Lives here, with the rail it describes; `session-states` owns the RULE for
 *  moving it, because that rule is about panes and focus. Keeping the atom on
 *  this side of the line means the preview store still knows nothing about
 *  sessions, which is what lets it be mocked and tested on its own. */
export const $browserSessionId = atom<null | string>(null)

/** Is this tab part of the browser the conversation `sessionId` is showing?
 *
 *  UNOWNED TABS ALWAYS ARE, and that arm is not a nicety — it is the whole
 *  reason the last attempt at scoping this pane broke. Filtering the mirror by
 *  workspace mode once dropped the pane out of Bot Mode entirely, so
 *  `openPreview` ran and a clicked link looked like a no-op (see the note on
 *  `watchPreviewTileMirror`). Everything a person opens themselves — a file
 *  from the tree, an artifact card, a link in any chat — is unowned and belongs
 *  to no conversation, so it stays visible from all of them. Only an agent's
 *  own browser tabs are scoped, which is exactly what "this chat's browser"
 *  means. */
export function previewTabBelongsToSession(
  tab: PreviewTab,
  sessionId: null | string,
  storedSessionId?: null | string
): boolean {
  // A RESTORED tab has only `ownerKey` — its runtime died with the last run.
  // Matching the conversation's stored id is what keeps a browser with its own
  // chat across a restart instead of spilling into all of them.
  if (!tab.owner && tab.ownerKey) {
    return !sessionId || (Boolean(storedSessionId) && tab.ownerKey === storedSessionId)
  }

  // No conversation established yet (first paint, a window with no chat
  // focused): show everything. An empty rail is a worse answer than an
  // unscoped one, and this is the state the Bot Mode regression lived in.
  return !sessionId || !tab.owner || tab.owner === sessionId
}

/** Preview tabs that still belong in the layout tree (not popped out).
 *
 *  NOT scoped to a conversation, deliberately. This is what the pane mirror
 *  registers from, and dropping a tab out of it calls `removeTreePane` — which
 *  destroys the pane and the live page inside it. Scoping HERE would mean that
 *  glancing at another chat tore down the page the first chat's agent was in
 *  the middle of driving, losing its scroll, its form, its login, and then
 *  reloading it on the way back. Every preview tab therefore stays registered;
 *  which ones the STRIP shows is a visibility question, answered by hiding
 *  panes (`syncBrowserSessionPanes`), which keeps them mounted. */
export const $dockedPreviewTabs = computed([$previewTabs, $poppedBrowserTabIds], (tabs, popped) =>
  popped.size === 0 ? tabs : tabs.filter(tab => !popped.has(tab.id))
)

export const $previewReloadRequest = atom(0)
export const $previewServerRestart = atom<PreviewServerRestart | null>(null)
export const $previewServerRestartStatus = computed($previewServerRestart, restart => restart?.status ?? 'idle')

/** The tab that owns `target`. Files and artifacts are keyed by IDENTITY —
 *  the same file is always the same tab, reopening it re-fronts the one it
 *  already has. A URL has no identity here: a Browser tab is a vessel you
 *  navigate, so it is picked (`browserTabId`) rather than derived. */
export function previewTabId(target: PreviewTarget): RightRailTabId {
  return `${target.kind}:${target.url}`
}

const isBrowserTab = (tab: PreviewTab): boolean => tab.target.kind === 'url'

/** A Browser tab's id, minted the way a terminal's is — there is no identity to
 *  derive one from. Random rather than the lowest free slot: an id is never
 *  handed out twice, so per-tab state keyed by it (`$browserPages`, the console
 *  buffer) cannot resurface under a later tab if a close ever fails to wipe it. */
function mintBrowserTabId(): RightRailTabId {
  const unique =
    globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`

  return `url:browser-${unique}`
}

/** The Browser a URL should open in: the one you're looking at, else the one
 *  you used last. A link from chat navigates the browser you already have
 *  rather than stacking another identical tab — new tabs are something you
 *  ask for (the strip's "+"), the way they are in a real browser.
 *
 *  THE AGENT IS NOT YOU. Re-using "the tab you're looking at" is right for a
 *  link you clicked and wrong for a tool call: it silently replaced the page
 *  the person was reading with wherever the agent went next (#93190). So an
 *  agent open resolves against the agent's OWN tab and mints one when it has
 *  none — it browses beside you rather than over you. It still re-uses that
 *  one tab across a task, because a tab per navigation would bury the strip.
 *
 *  AND ANOTHER AGENT IS NOT THIS AGENT. "Its own tab" is per SESSION. Both
 *  lookups below used to match any agent tab, so the second conversation to
 *  open a page inherited the first one's tab — and because a browser tab id
 *  once derived from the url, two chats opening the SAME address collided by
 *  construction. Scoped to `sessionId`, a session with no tab of its own mints
 *  one, which is what makes two conversations two browsers. */
function browserTabId(
  tabs: PreviewTab[],
  source: PreviewRecordSource,
  url?: string,
  sessionId?: null | string
): RightRailTabId {
  if (source === 'tool-result') {
    // Opening a page one of its own tabs already shows means "go back to that
    // one" — the only way the agent can re-target a tab, and the reason
    // `new_tab` is no longer a one-way door.
    const holding = url
      ? tabs.find(tab => agentOwns(tab, sessionId ?? null) && tab.target.url === url)
      : undefined

    return (holding ?? agentTab(tabs, sessionId ?? null))?.id ?? mintBrowserTabId()
  }

  // Only tabs this conversation can actually see. Navigating a browser that is
  // filtered out of the strip is navigating a page nobody is looking at.
  const visible = tabs.filter(tab => previewTabBelongsToSession(tab, sessionId ?? null))
  const active = visible.find(tab => tab.id === $rightRailActiveTabId.get())

  if (active && isBrowserTab(active)) {
    return active.id
  }

  return visible.findLast(isBrowserTab)?.id ?? mintBrowserTabId()
}

// Browsing files is "peek at the source"; a tool or an explicit link handing
// you an HTML file means "run it".
function isFilePreviewSource(source: PreviewRecordSource): boolean {
  return source === 'file-browser' || source === 'manual'
}

function previewTargetForSource(target: PreviewTarget, source: PreviewRecordSource): PreviewTarget {
  if (target.kind !== 'file' || target.previewKind !== 'html' || target.renderMode === 'source') {
    return target
  }

  return { ...target, renderMode: isFilePreviewSource(source) ? 'source' : 'preview' }
}

/** Open (or re-front) the tab for `target`. Re-opening an existing tab refreshes
 *  its target so a stale label/path can't outlive the thing it points at. The
 *  only way anything reaches a preview. */
export function openPreview(
  target: PreviewTarget,
  source: PreviewRecordSource = 'manual',
  options: { newTab?: boolean; ownerKey?: null | string; reveal?: boolean; sessionId?: null | string } = {}
) {
  const resolved = previewTargetForSource(target, source)
  const current = $previewTabs.get()
  // `newTab` only means anything for a browser: file and artifact tabs are
  // addressed by their content, so a second tab on the same file would be the
  // same tab twice.
  const fresh = options.newTab && resolved.kind === 'url'
  const sessionId = options.sessionId ?? null

  const id = fresh
    ? mintBrowserTabId()
    : resolved.kind === 'url'
      ? browserTabId(current, source, resolved.url, sessionId)
      : previewTabId(resolved)

  const index = current.findIndex(tab => tab.id === id)
  // Ownership is the tab's, not the target's: it decides who may navigate this
  // tab later, so it has to outlive the open that created it. Sticky, because a
  // person opening a link in the agent's tab is visiting, not taking it over.
  const owned = current[index]?.agent || (resolved.kind === 'url' && source === 'tool-result')
  // Which agent, not just "an agent". Sticky the same way: a person visiting
  // the tab does not re-assign it, and an existing owner is not displaced by a
  // later session — `browserTabId` only returns a tab this session already
  // owns, so reaching here with a different owner means the user opened it.
  const owner = owned ? (current[index]?.owner ?? sessionId ?? undefined) : undefined
  const ownerKey = owned ? (current[index]?.ownerKey ?? options.ownerKey ?? undefined) : undefined
  const tab: PreviewTab = owned
    ? { agent: true, id, owner, ownerKey, target: resolved }
    : { id, target: resolved }

  if (owned) {
    agentTabBySession.set(owner ?? UNSCOPED, id)
  }

  $previewTabs.set(index === -1 ? [...current, tab] : current.map((item, i) => (i === index ? tab : item)))

  // Selecting is a claim on the rail, and the rail is one surface. A background
  // conversation opening a page must not yank you off the page you are reading
  // — the caller knows whether its session is on screen, so it says. Anything
  // the person did themselves (default) still fronts, as it always has.
  if (options.reveal !== false) {
    selectRightRailTab(id)
  }
}

const blankPage = (): PreviewTarget => ({ kind: 'url', label: 'Browser', source: 'about:blank', url: 'about:blank' })

/** Show the Browser — the surface, not a page. Keeps whatever it was last
 *  showing so the hotkey re-fronts your page instead of wiping it; with no
 *  browser open it lands on `about:blank`, where the pane's empty state
 *  invites an address. */
export function openBrowserTab(sessionId: null | string = $browserSessionId.get()) {
  // Point the rail at this conversation's browser BEFORE choosing a tab, or the
  // tab we front is one the strip is about to filter away.
  if (sessionId) {
    $browserSessionId.set(sessionId)
  }

  const tabs = $previewTabs.get()
  // This conversation's own tab first, then any browser it can see (a page you
  // opened yourself is everyone's). Selected directly rather than routed back
  // through `openPreview`: re-deriving the target's tab there resolved against
  // the WHOLE list, so asking for an empty conversation's browser navigated
  // whichever tab happened to be active — another chat's page — to about:blank.
  const current = agentTab(tabs, sessionId) ?? tabs.filter(tab => previewTabBelongsToSession(tab, sessionId)).findLast(isBrowserTab)

  if (current) {
    selectRightRailTab(current.id)

    return
  }

  newBrowserTab(sessionId)
}

/** Another Browser, always — the strip's "+". It joins the browser you are
 *  looking at, which is a conversation's, so it belongs to that conversation
 *  rather than becoming a stray shared tab. */
export function newBrowserTab(sessionId: null | string = $browserSessionId.get()) {
  const id = mintBrowserTabId()

  $previewTabs.set([...$previewTabs.get(), { agent: true, id, owner: sessionId ?? undefined, target: blankPage() }])
  selectRightRailTab(id)
}

export function closeRightRailTab(tabId: string) {
  const current = $previewTabs.get()
  const index = current.findIndex(tab => tab.id === tabId)

  if (index === -1) {
    return
  }

  const next = current.filter(tab => tab.id !== tabId)

  $previewTabs.set(next)

  if ($rightRailActiveTabId.get() === tabId) {
    selectRightRailTab(next[Math.min(index, next.length - 1)]?.id ?? null)
  }

  if (next.length === 0) {
    selectRightRailTab(null)
  }
}

/** Close the tab showing `source`, if one is open. Returns whether it closed. */
export function closePreviewForSource(source: string): boolean {
  return closePreviewMatching(source)
}

/** Close the first tab whose source, url, or label matches any candidate.
 *  Empty candidates are a no-op so a missed match cannot wipe the rail —
 *  closing the whole pane is `closeRightRail`. */
export function closePreviewMatching(...candidates: string[]): boolean {
  const queries = [...new Set(candidates.map(value => value.trim()).filter(Boolean))]

  if (queries.length === 0) {
    return false
  }

  const tab = $previewTabs.get().find(item => {
    const fields = [item.target.source, item.target.url, item.target.label]

    return queries.some(query => fields.includes(query))
  })

  if (!tab) {
    return false
  }

  closeRightRailTab(tab.id)

  return true
}

/** Artifact tabs can't outlive the registry they read from, so clearing it
 *  closes them. File and URL tabs re-read from their source and are left alone. */
export function closeArtifactPreviewTabs() {
  for (const tab of $previewTabs.get()) {
    if (tab.target.kind === 'artifact') {
      closeRightRailTab(tab.id)
    }
  }
}

/** Close every tab so the rail's panes leave the tree. */
export function closeRightRail() {
  $previewTabs.set([])
  selectRightRailTab(null)
}

export function requestPreviewReload() {
  $previewReloadRequest.set($previewReloadRequest.get() + 1)
}

export function beginPreviewServerRestart(taskId: string, url: string) {
  $previewServerRestart.set({ status: 'running', taskId, url })
}

export function completePreviewServerRestart(taskId: string, text: string) {
  const current = $previewServerRestart.get()

  if (current?.taskId !== taskId) {
    return
  }

  $previewServerRestart.set({
    ...current,
    message: text,
    status: normalize(text).startsWith('error:') ? 'error' : 'complete'
  })
}

export function progressPreviewServerRestart(taskId: string, text: string) {
  const current = $previewServerRestart.get()

  if (current?.taskId !== taskId || current.status !== 'running') {
    return
  }

  $previewServerRestart.set({
    ...current,
    message: text
  })
}

export function failPreviewServerRestart(taskId: string, message: string) {
  const current = $previewServerRestart.get()

  if (current?.taskId !== taskId || current.status !== 'running') {
    return
  }

  $previewServerRestart.set({
    ...current,
    message,
    status: 'error'
  })
}
