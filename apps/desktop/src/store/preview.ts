import { atom, computed } from 'nanostores'

import { persistentAtom } from '@/lib/persisted'
import { normalize } from '@/lib/text'

import { $rightRailActiveTabId, type RightRailTabId, selectRightRailTab } from './layout'
import { $focusedStoredSessionId } from './session-states'

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
  id: RightRailTabId
  target: PreviewTarget
  /** The session that opened the tab. Absent only on legacy rows written
   *  before session scoping (decodePreviewTabs migrates those to pinned). */
  sessionId?: string
  /** Pinned tabs render in EVERY session — the explicit cross-session
   *  workspace. Everything else is visible only in the session that opened it. */
  pinned?: boolean
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

  const tabs = (Array.isArray(parsed) ? parsed.filter(isPreviewTab) : []).map(tab =>
    isPdfFileTarget(tab.target) && tab.target.previewKind === 'binary'
      ? { ...tab, target: { ...tab.target, previewKind: 'pdf' as const } }
      : tab
  )

  // Legacy rows (written before session scoping) have no owner and no way to
  // recover one — keep them as workspace-pinned rather than dropping them or
  // dumping every stale tab into one chat. Explicit `!== undefined` checks:
  // a persisted `pinned: false` must not be re-pinned.
  const owned = tabs.map(tab =>
    tab.sessionId !== undefined || tab.pinned !== undefined ? tab : { ...tab, pinned: true }
  )

  // One Browser: rekey restored URL tabs onto the singleton id (rows written
  // before the id existed carried one id per address). File tabs rekey onto
  // their session-scoped canonical id. Keep only the LAST row per id — the
  // most recently opened wins.
  const lastUrl = owned.findLast(tab => tab.target.kind === 'url')
  const deduped = new Map<string, PreviewTab>()

  for (const tab of owned) {
    if (tab.target.kind === 'url' && tab !== lastUrl) {
      continue
    }

    const id =
      tab.target.kind === 'url' || tab.target.kind === 'file'
        ? previewTabId(tab.target, tab.sessionId)
        : tab.id

    deduped.set(id, { ...tab, id })
  }

  return [...deduped.values()]
}

export const $previewTabs = persistentAtom<PreviewTab[]>(TABS_STORAGE_KEY, [], {
  decode: decodePreviewTabs,
  // Inline bytes are not restorable. Strip them from images, and skip remote
  // HTML and artifact tabs that cannot render without their in-memory payload.
  encode: tabs =>
    JSON.stringify(
      tabs.filter(
        tab =>
          tab.target.kind !== 'artifact' &&
          !tab.target.transient &&
          !(tab.target.previewKind === 'html' && tab.target.dataUrl)
      ),
      (key, value) => (key === 'dataUrl' ? undefined : value)
    )
})

if (typeof window !== 'undefined') {
  try {
    window.localStorage.removeItem(LEGACY_SESSION_REGISTRY_KEY)
  } catch {
    // Storage access can throw in locked-down contexts; nothing depends on it.
  }
}

/** Tabs the ACTIVE session sees: its own tabs plus everything pinned. The
 *  layout-tree mirror renders only these, so a session switch swaps the drawer
 *  and pinned tabs are the explicit cross-session workspace. While no session
 *  exists (a fresh draft), ownerless tabs stay visible. */
export const $visiblePreviewTabs = computed(
  [$previewTabs, $focusedStoredSessionId],
  (tabs, sessionId) =>
    tabs.filter(
      tab => tab.pinned || tab.sessionId === sessionId || (sessionId == null && tab.sessionId == null)
    )
)

// A fresh draft has no session yet, so tabs opened there are ownerless — adopt
// them into the session the moment one exists (rekeying file ids onto the
// session-scoped form, so a later open of the same file dedupes), or the
// drawer would silently lose them at first send.
$focusedStoredSessionId.listen(sessionId => {
  if (sessionId == null) {
    return
  }

  const tabs = $previewTabs.get()

  if (tabs.some(tab => tab.sessionId == null && !tab.pinned)) {
    const activeId = $rightRailActiveTabId.get()
    let newActiveId = activeId

    const updated = tabs.map(tab => {
      if (tab.sessionId != null || tab.pinned) {
        return tab
      }

      const nextId = tab.target.kind === 'file' ? previewTabId(tab.target, sessionId) : tab.id

      if (tab.id === activeId) {
        newActiveId = nextId
      }

      return { ...tab, id: nextId, sessionId }
    })

    // A rekey can collide with an already session-scoped row of the same file
    // (defensive: adoption runs synchronously with the focus change, so a real
    // open can't interleave — but keep the no-duplicate invariant explicit).
    const deduped = new Map<string, PreviewTab>()

    for (const tab of updated) {
      deduped.set(tab.id, tab)
    }

    $previewTabs.set([...deduped.values()])

    if (newActiveId !== activeId) {
      selectRightRailTab(newActiveId)
    }
  }
})

/** The tab the rail actually shows. A stale or missing selection falls back to
 *  the first tab, so the strip, `⌘W`, and the pane never disagree about which
 *  tab is on screen. */
function resolveActiveTab(tabs: PreviewTab[], activeTabId: RightRailTabId | null): PreviewTab | null {
  return tabs.find(tab => tab.id === activeTabId) ?? tabs[0] ?? null
}

function activePreviewTab(): PreviewTab | null {
  return resolveActiveTab($visiblePreviewTabs.get(), $rightRailActiveTabId.get())
}

// A restored active id whose tab didn't survive validation would leave the rail
// pointing at nothing.
selectRightRailTab(activePreviewTab()?.id ?? null)

/** The target the rail is currently showing, or null when it has no tabs. */
export const $previewTarget = computed(
  [$visiblePreviewTabs, $rightRailActiveTabId],
  (tabs, activeTabId) => resolveActiveTab(tabs, activeTabId)?.target ?? null
)

/** Raw `source` strings of every tab the active session sees, for the composer
 *  rows that toggle a preview open and closed by the target they were handed. */
export const $previewTabSources = computed($visiblePreviewTabs, tabs => tabs.map(tab => tab.target.source))

export const $previewReloadRequest = atom(0)
export const $previewServerRestart = atom<PreviewServerRestart | null>(null)
export const $previewServerRestartStatus = computed($previewServerRestart, restart => restart?.status ?? 'idle')

/** The one Browser tab's id. URL targets all share it: the tab names the
 *  SURFACE (Browser), not the page, so opening a second URL navigates the
 *  browser it already has — re-front the tab, swap its target, and the pane
 *  rebuilds its webview against the new url. Files and artifacts stay keyed
 *  by identity; only the web surface is a singleton. */
const BROWSER_TAB_ID: RightRailTabId = 'url:browser'

/** A file tab's identity is its canonical path, scoped to the session that
 *  opened it — the same file opened in two conversations must be two tabs,
 *  each owned by its session. The `file://` scheme is stripped so the two
 *  entry points (a `file://` URL vs a plain path) resolve to the same key. */
function canonicalFileKey(target: PreviewTarget): string {
  return (target.path || target.url).replace(/^file:\/\//, '')
}

export function previewTabId(target: PreviewTarget, sessionId?: null | string): RightRailTabId {
  if (target.kind === 'url') {
    return BROWSER_TAB_ID
  }

  if (target.kind === 'file') {
    return `file:${sessionId ? `${sessionId}:` : ''}${canonicalFileKey(target)}`
  }

  return `${target.kind}:${target.url}`
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
export function openPreview(target: PreviewTarget, source: PreviewRecordSource = 'manual') {
  const resolved = previewTargetForSource(target, source)
  const sessionId = $focusedStoredSessionId.get() ?? undefined
  const id = previewTabId(resolved, sessionId)
  const current = $previewTabs.get()
  const index = current.findIndex(tab => tab.id === id)

  // A PINNED row for the same file carries the unprefixed (legacy) id, so a
  // session-scoped open would otherwise stack a second tab for the same file.
  // Reuse the pinned row instead — refreshed, still pinned, fronted. Never
  // steal another session's owned tab: that coexistence is the point.
  const existing =
    index === -1 && resolved.kind === 'file'
      ? current.find(
          tab => tab.pinned && tab.target.kind === 'file' && canonicalFileKey(tab.target) === canonicalFileKey(resolved)
        )
      : current[index]

  // Reuse semantics depend on WHY the row was found:
  // - Same-session re-open (any kind): keep the owner and pin state — only
  //   the target refreshes.
  // - A PINNED file row reached from ANOTHER session: keep its owner (and
  //   therefore its owner-keyed id) — the pin is the explicit cross-session
  //   workspace contract, and rekeying to the opening session would leave
  //   `id` and `sessionId` disagreeing until the next unpin (which returns
  //   the tab to that owner anyway).
  // - The singleton Browser or an artifact reached from another session: the
  //   surface NAVIGATES, so it re-owns to the opening session — otherwise the
  //   new URL would stay invisible in the session that just opened it.
  // - Ownerless legacy rows: adopted — rekeyed and stamped — into the opening
  //   session (see the pinned-legacy test).
  // The id is derived from the FINAL session id so the two can never split.
  const tabSessionId =
    existing?.sessionId == null || existing.pinned || existing.sessionId === sessionId
      ? existing?.sessionId ?? sessionId
      : sessionId

  const tab: PreviewTab = {
    id: previewTabId(resolved, tabSessionId),
    target: resolved,
    sessionId: tabSessionId,
    pinned: existing?.pinned
  }

  const replaceIndex = existing ? current.indexOf(existing) : -1

  $previewTabs.set(replaceIndex === -1 ? [...current, tab] : current.map((item, i) => (i === replaceIndex ? tab : item)))
  // Select the row's FINAL id: when a pinned row is reused from another
  // session, the id keeps the owner's session, so the pre-computed id would
  // point at nothing.
  selectRightRailTab(tab.id)
}

/** Pin or unpin a preview tab. Pinned tabs render in EVERY session — the
 *  explicit cross-session workspace; unpinning returns it to its session
 *  (adopting the current one, and rekeying the file id, when the tab never
 *  had an owner). */
export function setPreviewTabPinned(tabId: string, pinned: boolean): void {
  const currentSession = $focusedStoredSessionId.get() ?? undefined
  const activeId = $rightRailActiveTabId.get()
  let newActiveId = activeId

  $previewTabs.set(
    $previewTabs.get().map(tab => {
      if (tab.id !== tabId) {
        return tab
      }

      const nextSession = tab.sessionId ?? (!pinned ? currentSession : undefined)
      const nextId = tab.target.kind === 'file' ? previewTabId(tab.target, nextSession) : tab.id

      if (tab.id === activeId) {
        newActiveId = nextId
      }

      return { ...tab, id: nextId, pinned, sessionId: nextSession }
    })
  )

  if (newActiveId !== activeId) {
    selectRightRailTab(newActiveId)
  }
}

/** Drop the tabs a deleted session opened. Pinned tabs survive — they belong
 *  to the workspace, not the session that opened them. */
export function prunePreviewTabsForSession(sessionId: string): void {
  $previewTabs.set($previewTabs.get().filter(tab => tab.pinned || tab.sessionId !== sessionId))
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

/** Close the tab showing `source` in the CURRENT session, if one is open.
 *  Returns whether it closed. */
export function closePreviewForSource(source: string): boolean {
  const tab = $visiblePreviewTabs.get().find(item => item.target.source === source)

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
