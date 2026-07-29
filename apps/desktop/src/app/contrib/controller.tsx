import { useStore } from '@nanostores/react'
import { computed } from 'nanostores'
import type { CSSProperties, ReactElement, PointerEvent as ReactPointerEvent } from 'react'

import { $artifactsPaneOpen, closeArtifactsPane, openArtifactsPane } from '@/app/artifacts/pane-state'
import { PREVIEW_RAIL_MAX_WIDTH, PREVIEW_RAIL_MIN_WIDTH } from '@/app/chat/right-rail'
import { SessionStatusDot } from '@/app/chat/session-status-dot'
import { PALETTE_AREA, type PaletteContribution } from '@/app/command-palette/contrib'
import { RightPanelPicker } from '@/app/right-panel/picker'
import { type StatusbarItem } from '@/app/shell/statusbar-controls'
import { IdleMount } from '@/components/idle-mount'
import { toggleLayoutEditMode } from '@/components/pane-shell/edit-mode'
import { allPaneIds, group, groupLeafIds, split } from '@/components/pane-shell/tree/model'
import { LayoutTreeRoot } from '@/components/pane-shell/tree/renderer'
import type { DoubleTapContext } from '@/components/pane-shell/tree/renderer/drag-session'
import {
  $activePresetId,
  $layoutTree,
  $userPlacedPanes,
  bindTreeSideVisibility,
  declareDefaultTree,
  dismissTreePane,
  markCollapsePane,
  mirrorLayoutTree,
  paneRootSide,
  registerLayoutResetHandler,
  registerPaneCloser,
  registerPaneOpener,
  resetLayoutTree,
  revealTreePane,
  setPaneCollapsed,
  setTreePaneHidden,
  watchContributedPanes
} from '@/components/pane-shell/tree/store'
import { SidebarProvider } from '@/components/ui/sidebar'
import { discoverBundledPlugins } from '@/contrib/plugins'
import { Slot } from '@/contrib/react/slot'
import { useContributions } from '@/contrib/react/use-contributions'
import { registry } from '@/contrib/registry'
import { discoverRuntimePlugins } from '@/contrib/runtime-loader'
import { sessionTitle as storedSessionTitle } from '@/lib/chat-runtime'
import { LayoutDashboard, PanelBottom } from '@/lib/icons'
import { type KeybindContribution, KEYBINDS_AREA } from '@/lib/keybinds/actions'
import { Codecs, persistentAtom } from '@/lib/persisted'
import { pruneComposerPopoutZones } from '@/store/composer-popout'
import {
  $fileBrowserOpen,
  $panesFlipped,
  $sidebarOpen,
  FILE_BROWSER_DEFAULT_WIDTH,
  FILE_BROWSER_MAX_WIDTH,
  FILE_BROWSER_MIN_WIDTH,
  setFileBrowserOpen,
  setSidebarOpen,
  SIDEBAR_DEFAULT_WIDTH,
  SIDEBAR_MAX_WIDTH
} from '@/store/layout'
import { $previewPaneOpen, $previewTabs, hidePreviewPane } from '@/store/preview'
import { $reviewOpen, hideReviewPane, REVIEW_PANE_ID } from '@/store/review'
import { $rightPanelOpen, $rightPanelRevealRequest, setRightPanelOpen } from '@/store/right-panel'
import { $currentCwd, $selectedStoredSessionId, $sessions, sessionMatchesStoredId } from '@/store/session'
import { watchSessionPins } from '@/store/session-pin-sync'
import { $statusbarVisible, toggleStatusbarVisible } from '@/store/statusbar-prefs'

import type { SessionDragPayload } from '../chat/composer/inline-refs'
import { watchRouteTiles } from '../chat/route-tile'
import { startSessionDrag } from '../chat/session-drag'
import {
  SessionTileCloseConfirm,
  stackSessionTilesIntoMain,
  watchSessionTiles,
  WorkspaceTabMenu
} from '../chat/session-tile'
import { shouldMigrateLegacyRightPanel } from '../right-panel/layout-migration'
import { $terminalTakeover, setTerminalTakeover } from '../right-sidebar/store'
import { $workspaceIsPage } from '../routes'

import { ArtifactsPaneContent, FilesPane, LogsPane, PreviewRailPane, ReviewPaneContent } from './panes'
import { ContribWiring, WiredPane } from './wiring'

/**
 * Stripped-down app root (bb/contrib-areas) on the layout TREE model, mounting
 * the REAL app surfaces. The title bar and status bar sit OUTSIDE the grid
 * (fixed chrome) but are fully composable: title bar renders `titleBar.left/
 * right` slots; the status bar consumes `statusBar.left/right` DATA
 * contributions (payload = StatusbarItem). Core registers its items through
 * the same calls a plugin would use.
 */

// ---------------------------------------------------------------------------
// Pane contributions. `data.placement` = semantic role for grid presets;
// `data.minWidth/maxWidth/minHeight/maxHeight` = the SAME clamps the app's
// `Pane` props declare — the layout tree sizes zones by weight (percentage)
// but a zone never shrinks/grows past its active pane's clamp.
// Headers are contextual (tree-side): a pane alone in a zone shows no
// header/tab by default; stacked panes show chips. Double-click a zone
// toggles its header either way.
// ---------------------------------------------------------------------------

// ONE render identity for the workspace pane — syncWorkspaceTitle re-registers
// the contribution (new title) and a fresh closure would remount the chat.
const renderWorkspacePane = () => <WiredPane part="chatRoutes" />

// Boot-hidden panes mount behind display:none (instant-toggle contract) — defer
// them to idle so they're off the first-paint path, warm before reveal.
const idle = (node: ReactElement) => <IdleMount>{node}</IdleMount>
const renderRightPanelPicker = () => <RightPanelPicker compact />
// The functional work area is usually paired with the outer Files sidebar.
// Keep enough room for previews without consuming the composer at laptop
// widths; large windows still grow it to a comfortable document column.
const RIGHT_PANEL_WIDTH = 'clamp(20rem, 28vw, 30rem)'
// The main tab carries the same session context menu as tile tabs (targets
// the loaded primary session; no menu on a fresh draft).
const wrapWorkspaceTab = (tab: ReactElement) => <WorkspaceTabMenu>{tab}</WorkspaceTabMenu>

/** The `@session` payload for the workspace tab — the loaded primary session,
 *  or null on a fresh draft / full-page view (nothing to link). */
const workspaceDragPayload = (): SessionDragPayload | null => {
  const selected = $selectedStoredSessionId.get()

  if (!selected || $workspaceIsPage.get()) {
    return null
  }

  const stored = $sessions.get().find(s => sessionMatchesStoredId(s, selected))

  return { id: selected, profile: stored?.profile ?? '', title: stored ? storedSessionTitle(stored) : '' }
}

// The main tab drags like a session tile — drop it on a composer to link the
// chat, on a zone/edge to stack/split. Defers (`false`) to the generic pane
// move when there's no loaded session to carry.
const workspaceTabDrag = (event: ReactPointerEvent<HTMLElement>, onTap: () => void, double?: DoubleTapContext) => {
  const payload = workspaceDragPayload()

  if (!payload) {
    return false
  }

  startSessionDrag(payload, event, { double, onTap })

  return true
}

registry.registerMany([
  {
    id: 'sessions',
    area: 'panes',
    title: 'sessions',
    // Collapsible: leaves the grid on narrow viewports (edge overlay instead).
    // dock: where a RE-ADOPTED pane lands (healed from a stale dismissal) —
    // its default-ish spot beside main, not a random same-placement stack.
    data: {
      placement: 'left',
      collapsible: true,
      dock: { pane: 'workspace', pos: 'left' },
      revealAliases: ['chat-sidebar'],
      width: `${SIDEBAR_DEFAULT_WIDTH}px`,
      minWidth: `${SIDEBAR_DEFAULT_WIDTH}px`,
      maxWidth: `${SIDEBAR_MAX_WIDTH}px`
    },
    render: () => <WiredPane part="sidebar" />
  },
  {
    id: 'workspace',
    area: 'panes',
    // Live-retitled to the loaded session by syncWorkspaceTitle below.
    title: 'New session',
    data: {
      placement: 'main',
      minWidth: '22vw',
      tabDrag: workspaceTabDrag,
      tabWrap: wrapWorkspaceTab,
      uncloseable: true
    },
    render: renderWorkspacePane
  },
  {
    id: 'terminal',
    area: 'panes',
    title: 'terminal',
    // revealOnPreset: choosing a layout that places the terminal (e.g.
    // "Terminal deck") turns takeover on so the zone actually shows, instead of
    // staying collapsed behind the ⌃` toggle. height sizes the fixed track (a
    // single-pane zone declaring a height is a fixed track — the preset weight
    // is moot): a short deck, not a third of the window.
    data: {
      placement: 'right',
      collapsible: true,
      dock: { pane: 'preview', pos: 'center' },
      forceHeader: true,
      minimizable: false,
      tabBarAction: renderRightPanelPicker,
      width: RIGHT_PANEL_WIDTH,
      minWidth: PREVIEW_RAIL_MIN_WIDTH,
      maxWidth: PREVIEW_RAIL_MAX_WIDTH,
      height: '20vh',
      minHeight: '7.5rem',
      maxHeight: '80vh',
      revealOnPreset: true
    },
    render: () => <WiredPane part="terminal" />
  },
  {
    id: 'files',
    area: 'panes',
    title: 'files',
    // Files is structural chrome, not a right-tools function tab. It owns the
    // narrow outermost sidebar while Preview/Review/Artifacts/Terminal share
    // the wider work area immediately to its inside.
    data: {
      placement: 'right',
      collapsible: true,
      dock: { pane: 'workspace', pos: 'right' },
      revealAliases: ['file-browser'],
      width: FILE_BROWSER_DEFAULT_WIDTH,
      minWidth: FILE_BROWSER_MIN_WIDTH,
      maxWidth: FILE_BROWSER_MAX_WIDTH
    },
    render: () => idle(<FilesPane />)
  },
  {
    id: 'preview',
    area: 'panes',
    title: 'preview',
    // The rail brings its OWN tab strip (per-target tabs with close buttons).
    // Exists only while something is previewed — visibility is bound to the
    // preview targets below, like every other self-managed surface. If a stale
    // persisted tree is missing it, re-adopt it as a new zone left of Files;
    // the other functions then stack back into this zone.
    data: {
      placement: 'right',
      collapsible: true,
      dock: { pane: 'files', pos: 'left' },
      forceHeader: true,
      minimizable: false,
      tabBarAction: renderRightPanelPicker,
      width: RIGHT_PANEL_WIDTH,
      minWidth: PREVIEW_RAIL_MIN_WIDTH,
      maxWidth: PREVIEW_RAIL_MAX_WIDTH
    },
    render: () => idle(<PreviewRailPane />)
  },
  {
    id: 'artifacts-pane',
    area: 'panes',
    title: 'artifacts',
    data: {
      placement: 'right',
      collapsible: true,
      dock: { pane: 'preview', pos: 'center' },
      forceHeader: true,
      minimizable: false,
      tabBarAction: renderRightPanelPicker,
      width: RIGHT_PANEL_WIDTH,
      minWidth: PREVIEW_RAIL_MIN_WIDTH,
      maxWidth: PREVIEW_RAIL_MAX_WIDTH
    },
    render: () => idle(<ArtifactsPaneContent />)
  },
  {
    id: 'review',
    area: 'panes',
    title: 'review',
    // Hidden until ⌘G ($reviewOpen); when present it shares the functional
    // right-tools group rather than adding another sidebar track.
    data: {
      placement: 'right',
      collapsible: true,
      dock: { pane: 'preview', pos: 'center' },
      forceHeader: true,
      minimizable: false,
      tabBarAction: renderRightPanelPicker,
      revealAliases: [REVIEW_PANE_ID],
      width: RIGHT_PANEL_WIDTH,
      minWidth: PREVIEW_RAIL_MIN_WIDTH,
      maxWidth: PREVIEW_RAIL_MAX_WIDTH
    },
    render: () => idle(<ReviewPaneContent />)
  },
  {
    // Optional chrome — in NO default layout. Adoption stacks it with the
    // terminal; $logsOpen (default off, ⌘K "Toggle logs") reveals it.
    id: 'logs',
    area: 'panes',
    title: 'logs',
    // revealOnPreset: the Quad layout places logs, so applying it turns the
    // logs pane on (like a ⌘K "Toggle logs") instead of leaving it collapsed.
    data: { placement: 'bottom', height: '20vh', minHeight: '7.5rem', maxHeight: '80vh', revealOnPreset: true },
    render: () => idle(<LogsPane />)
  }
])

// ---------------------------------------------------------------------------
// Chrome contributions. The title bar and status bar are fixed chrome outside
// the grid, composable through these areas. Everything real lives in the real
// components (TitlebarControls / useStatusbarItems). Sample PLUGIN
// contributions don't live here — they're their own files under `src/plugins/`,
// auto-discovered by discoverBundledPlugins() below.
// ---------------------------------------------------------------------------

registry.registerMany([
  // Titlebar center stays empty on purpose: session title lives in tabs +
  // sidebar; place/cwd lives in the sidebar project tree. Center is drag
  // chrome (plugins can still contribute to titleBar.center if needed).
  // Layout edit mode registers through the SAME declarative surfaces plugins
  // use: a rebindable keybind (collision-checked in the panel) + a ⌘K row
  // whose hotkey hint tracks the live binding.
  {
    id: 'layout.editMode',
    area: KEYBINDS_AREA,
    data: {
      id: 'layout.editMode',
      label: 'Toggle layout edit mode',
      defaults: ['mod+shift+\\'],
      run: toggleLayoutEditMode
    } satisfies KeybindContribution
  },
  {
    id: 'layout.editMode',
    area: PALETTE_AREA,
    data: {
      id: 'layout.editMode',
      label: 'Toggle layout edit mode',
      action: 'layout.editMode',
      icon: LayoutDashboard,
      keywords: ['layout', 'zones', 'panes', 'edit', 'rearrange'],
      run: toggleLayoutEditMode
    } satisfies PaletteContribution
  },
  // The agent's write -> see loop: rescan <hermes home>/desktop-plugins
  // without relaunching (same-id reloads dispose the previous incarnation).
  {
    id: 'plugins.reload',
    area: PALETTE_AREA,
    data: {
      id: 'plugins.reload',
      label: 'Reload desktop plugins',
      keywords: ['plugins', 'reload', 'refresh', 'desktop'],
      run: () => void discoverRuntimePlugins()
    } satisfies PaletteContribution
  },
  {
    id: 'layout.reset',
    area: PALETTE_AREA,
    data: {
      id: 'layout.reset',
      label: 'Reset layout',
      icon: LayoutDashboard,
      keywords: ['layout', 'reset', 'default', 'panes'],
      run: resetLayoutTree
    } satisfies PaletteContribution
  },
  // Hiding the bar removes the surface that would otherwise offer it back, so
  // ⌘K is the guaranteed door in (alongside the rebindable ⌘⇧S).
  {
    id: 'view.toggleStatusbar',
    area: PALETTE_AREA,
    data: {
      id: 'view.toggleStatusbar',
      label: 'Toggle status bar',
      action: 'view.toggleStatusbar',
      icon: PanelBottom,
      keywords: ['status bar', 'statusbar', 'bottom bar', 'hide', 'show', 'chrome'],
      run: toggleStatusbarVisible
    } satisfies PaletteContribution
  },
  // The keybind panel's non-titlebar door (the keyboard icon is gone).
  {
    id: 'keybinds.panel',
    area: PALETTE_AREA,
    data: {
      id: 'keybinds.panel',
      label: 'Keyboard shortcuts',
      keywords: ['keybinds', 'shortcuts', 'hotkeys', 'keyboard'],
      run: () => window.dispatchEvent(new CustomEvent('hermes:open-keybinds'))
    } satisfies PaletteContribution
  }
])

// ---------------------------------------------------------------------------
// Layout presets — CHAT (main) always dominates.
// ---------------------------------------------------------------------------

// The right side has two roles:
//
//   Workspace | right-tools | Files
//
// Review, Artifacts, Preview and Terminal are mutually exclusive top-level
// function tabs in one wider work area. Files stays a narrow structural
// sidebar at the outer edge, so choosing a file opens Preview directly to its
// left without replacing the tree. This caps the default at two right tracks
// instead of the former one-track-per-tool squeeze.
const DEFAULT_TREE = split(
  'row',
  [
    group(['sessions'], { id: 'grp-sessions' }),
    group(['workspace'], { id: 'grp-main' }),
    group(['review', 'artifacts-pane', 'preview', 'terminal'], {
      active: 'preview',
      headerHidden: false,
      id: 'grp-right-tools'
    }),
    group(['files'], { id: 'grp-files' })
  ],
  [1, 3.4, 1.5, 0.8],
  'spl-root'
)

const FOCUS_TREE = split(
  'row',
  [group(['sessions']), group(['workspace', 'files', 'review', 'artifacts-pane', 'preview', 'terminal'])],
  [1, 4.6]
)

const TERMINAL_TREE = split(
  'column',
  [
    split('row', [group(['sessions']), group(['workspace']), group(['files', 'preview', 'review'])], [1, 3.2, 1.2]),
    group(['terminal'])
  ],
  [3, 1]
)

const QUAD_TREE = split(
  'column',
  [
    split('row', [group(['sessions', 'files']), group(['workspace'])], [1, 3]),
    split('row', [group(['terminal']), group(['preview', 'review', 'logs'])], [1.4, 1])
  ],
  [3, 1]
)

registry.registerMany([
  { id: 'default', area: 'layouts', title: 'Default', order: 0, data: DEFAULT_TREE },
  { id: 'focus', area: 'layouts', title: 'Focus', order: 10, data: FOCUS_TREE },
  { id: 'terminal-deck', area: 'layouts', title: 'Terminal deck', order: 20, data: TERMINAL_TREE },
  { id: 'quad', area: 'layouts', title: 'Quad', order: 30, data: QUAD_TREE }
])

declareDefaultTree(DEFAULT_TREE, {
  migratePersisted: current => shouldMigrateLegacyRightPanel(current, $activePresetId.get(), $userPlacedPanes.get())
})

// Bundled plugins load AFTER core, so a same-id contribution from a plugin
// deliberately overrides the core default (last writer wins). Third-party
// runtime plugins will flow through the same discovery seam.
discoverBundledPlugins()

// Plugin panes join the tree by their `placement` hint the moment they
// register — incl. runtime plugins arriving seconds after boot.
watchContributedPanes()

// Session + route (page) tiles: persisted splits register panes docked beside
// main.
watchSessionTiles()
watchRouteTiles()

// Composer pop-out state is keyed by layout zone, so drop entries for zones the
// user has since closed or merged away — otherwise a long-lived install keeps a
// row for every split it has ever had.
$layoutTree.subscribe(tree => {
  if (tree) {
    pruneComposerPopoutZones(groupLeafIds(tree))
  }
})

// Mirror sidebar pins into the backend keep-flag so the auto-archive sweep
// never hides a pinned chat (and pre-existing pins migrate transparently).
watchSessionPins()

// The main tab reads as its SESSION (the loaded title, "New session" on a
// fresh draft) — a stack of main + tiles is then just a row of session names.
// register() replaces same-id in place; the render fn is the shared constant
// above, so the pane content never remounts.
const syncWorkspaceTitle = () => {
  const selected = $selectedStoredSessionId.get()
  const stored = selected ? $sessions.get().find(s => sessionMatchesStoredId(s, selected)) : null

  registry.register({
    id: 'workspace',
    area: 'panes',
    title: stored ? storedSessionTitle(stored) : 'New session',
    data: {
      // The tab's status dot — the SAME primitive the sidebar row and session
      // tiles render, so the main tab never disagrees with its sidebar row. No
      // dot on a fresh draft (no session yet).
      tabLead: selected ? () => <SessionStatusDot session={stored} storedSessionId={selected} /> : undefined,
      // Pages aren't tab-able: the main zone's bar stands down while one shows.
      headerVeto: $workspaceIsPage.get(),
      placement: 'main',
      minWidth: '22vw',
      tabDrag: workspaceTabDrag,
      tabWrap: wrapWorkspaceTab,
      uncloseable: true
    },
    render: renderWorkspacePane
  })
}

$selectedStoredSessionId.listen(syncWorkspaceTitle)
$sessions.listen(syncWorkspaceTitle)
$workspaceIsPage.listen(syncWorkspaceTitle)

// Layout reset collapses every session tile into main as a tab (after the
// workspace) instead of re-scattering them — pre-placed before adoption.
registerLayoutResetHandler(stackSessionTilesIntoMain)

// ---------------------------------------------------------------------------
// Titlebar chrome toggles -> tree. The TitlebarControls buttons keep their
// store semantics ($sidebarOpen / $fileBrowserOpen / $panesFlipped); the tree
// reacts — a hidden pane's zone collapses (content stays mounted), the flip
// toggle mirrors the root row.
// ---------------------------------------------------------------------------

function bindPaneVisibility(
  paneId: string,
  $open: { get(): boolean; listen(fn: (open: boolean) => void): void },
  close?: () => void,
  open?: () => void
) {
  setTreePaneHidden(paneId, !$open.get())
  $open.listen(isOpen => setTreePaneHidden(paneId, !isOpen))

  // The tab menu's Close routes through the owning store (never dismissal),
  // so the pane's toggle buttons stay truthful.
  if (close) {
    registerPaneCloser(paneId, close)
  }

  // The opener is the mirror: preset application (revealOnPreset) shows the
  // pane through the same store, so the toggle stays truthful.
  if (open) {
    registerPaneOpener(paneId, open)
  }
}

// TOOL PANELS (terminal, logs): like bindPaneVisibility but the toggle COLLAPSES
// the zone to a persistent rail (tab stays) instead of hiding it — the
// IntelliJ/VS-Code tool-window model. Restore routes back through `open` (rail
// click / chevron) so ⌃`/the button stay truthful; the tab's ✕ removes it.
function bindPaneCollapse(
  paneId: string,
  $open: { get(): boolean; listen(fn: (open: boolean) => void): void },
  close: () => void,
  open: () => void
) {
  markCollapsePane(paneId)
  setPaneCollapsed(paneId, !$open.get())
  $open.listen(isOpen => setPaneCollapsed(paneId, !isOpen))
  registerPaneCloser(paneId, close)
  registerPaneOpener(paneId, open)
}

// SIDES have one source of truth: the TREE. The legacy $panesFlipped flag is
// DERIVED from where the sessions zone actually sits (TitlebarControls maps
// its left/right buttons through it), so dragging sessions across — or
// applying a mirrored preset — remaps the buttons automatically. The flip
// action (⌘\ / titlebar) mirrors the tree only when they disagree.
const sessionsOnRight = () => {
  const tree = $layoutTree.get()

  if (!tree) {
    return null
  }

  const order = allPaneIds(tree)
  const sessions = order.indexOf('sessions')
  const main = order.indexOf('workspace')

  return sessions >= 0 && main >= 0 ? sessions > main : null
}

$layoutTree.subscribe(() => {
  const flipped = sessionsOnRight()

  if (flipped !== null && flipped !== $panesFlipped.get()) {
    $panesFlipped.set(flipped)
  }
})

$panesFlipped.listen(flipped => {
  const current = sessionsOnRight()

  if (current !== null && current !== flipped) {
    mirrorLayoutTree()
  }
})

// POSITIONAL side toggles (titlebar buttons, ⌘B / ⌘J): $sidebarOpen ≙ the
// LEFT side of the main zone, $rightPanelOpen ≙ the RIGHT — everything on
// that side hides together, whatever panes have been rearranged there.
bindTreeSideVisibility('left', $sidebarOpen, setSidebarOpen)
bindTreeSideVisibility('right', $rightPanelOpen, setRightPanelOpen)

// Workspace-scoped surfaces: the file tree and git diff only mean something
// inside a project. A detached chat (no cwd) hides them — their zones
// collapse and the chat absorbs the width; picking a project brings them
// back. The terminal is NOT workspace-gated: unlike the old shell (where it
// rode the rail's row and vanished with it), its zone stands on its own.
const $hasWorkspace = computed($currentCwd, cwd => Boolean(cwd.trim()))

// Files owns the independent outer sidebar; the whole right region has a
// separate visibility flag.
bindPaneVisibility(
  'files',
  computed([$hasWorkspace, $fileBrowserOpen], (workspace, open) => workspace && open)
)
// ⌘G — the review sidebar appears/disappears (and comes to the front).
bindPaneVisibility(
  'review',
  computed([$reviewOpen, $hasWorkspace], (open, workspace) => open && workspace),
  hideReviewPane
)
// The terminal is a normal top-level function tab. Hiding it keeps the
// persistent xterm host and PTYs alive; reopening only reattaches the visible
// slot and fits the active inner terminal once.
bindPaneVisibility(
  'terminal',
  $terminalTakeover,
  () => setTerminalTakeover(false),
  () => setTerminalTakeover(true)
)

// Preview EXISTS only while something is previewed (old-shell semantics:
// closing the last preview tab closes the pane; a new target opens + fronts
// it). Same visibility binding as every other self-managed surface, driven
// by the open tabs instead of a toggle.
const $previewVisible = computed([$previewTabs, $previewPaneOpen], (tabs, open) => tabs.length > 0 && open)

bindPaneVisibility('preview', $previewVisible, hidePreviewPane)

bindPaneVisibility('artifacts-pane', $artifactsPaneOpen, closeArtifactsPane, openArtifactsPane)

$rightPanelRevealRequest.listen(({ paneId, sequence }) => {
  if (sequence === 0) {
    return
  }

  revealTreePane(paneId)
})

// Logs are optional chrome: off by default, toggled from ⌘K, persisted.
const $logsOpen = persistentAtom('hermes.desktop.logsOpen', false, Codecs.bool)

bindPaneCollapse(
  'logs',
  $logsOpen,
  () => $logsOpen.set(false),
  () => $logsOpen.set(true)
)
registry.register({
  id: 'logs.toggle',
  area: PALETTE_AREA,
  data: {
    id: 'logs.toggle',
    label: 'Toggle logs',
    keywords: ['logs', 'agent log', 'tail', 'debug'],
    run: () => $logsOpen.set(!$logsOpen.get())
  } satisfies PaletteContribution
})

// Sessions Close collapses its whole side (⌘B remains truthful). Files has its
// own visibility owner, so closing only the outer tree leaves right-tools
// available instead of collapsing Preview/Terminal with it.
registerPaneCloser('sessions', () =>
  paneRootSide('sessions') === 'left' ? setSidebarOpen(false) : dismissTreePane('sessions')
)
registerPaneCloser('files', () => setFileBrowserOpen(false))

// ---------------------------------------------------------------------------

interface TitlebarSlotProps {
  area: 'titleBar.center' | 'titleBar.left' | 'titleBar.right'
  className: string
  style?: CSSProperties
}

function TitlebarSlot({ area, className, style }: TitlebarSlotProps) {
  const items = useContributions(area)

  if (items.length === 0) {
    return null
  }

  return (
    <div className={className} style={style}>
      <Slot area={area} />
    </div>
  )
}

export function ContribController() {
  const sidebarOpen = useStore($sidebarOpen)
  const statusbarVisible = useStore($statusbarVisible)

  return (
    <SidebarProvider
      className="h-screen min-h-0 flex-col bg-background"
      onOpenChange={setSidebarOpen}
      open={sidebarOpen}
      style={{ '--sidebar-width': '100%' } as CSSProperties}
    >
      <ContribWiring>
        <div
          className="flex h-screen min-h-0 w-screen flex-col bg-(--ui-bg-chrome) text-(--ui-text-primary)"
          style={{ '--titlebar-height': '0px' } as CSSProperties}
        >
          {/* Title bar: fixed chrome outside the grid, composable via slots.
              Layout contract (no contribution can break it):
                - a full-bar DRAG BASE underneath (pointer-events-none, like
                  AppShell's drag strips) — everywhere without content drags
                  the window;
                - each slot region is width-fit, no-drag, pointer-events-auto,
                  so every contribution is clickable by construction;
                - LEFT/RIGHT slots align to the MAIN PANE's geometry via the
                  tree-published --workspace-left/right vars (pure CSS, no rect
                  threading), clamped to clear the REAL TitlebarControls
                  clusters (fixed, z-70); center is truly window-centered. */}
          <div className="relative flex h-[34px] shrink-0 items-center bg-(--ui-sidebar-surface-background) text-xs">
            {/* Drag strips, AppShell-style: cut to AVOID the fixed control
                clusters instead of overlapping them — Electron's no-drag
                carve-out of fixed/transformed elements is unreliable, so a
                full-bar drag base kills their clicks. In-flow slot content
                still carves via its own no-drag wrapper (the same pattern as
                the app's session-title button). */}
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-y-0 left-0 w-(--titlebar-controls-left,14px) [-webkit-app-region:drag]"
            />
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-y-0 left-[calc(var(--titlebar-controls-left,14px)+(var(--titlebar-control-size,1.25rem)*2)+0.75rem)] right-[calc(var(--titlebar-tools-right,0.75rem)+var(--titlebar-tools-width,5.5rem)+0.75rem)] [-webkit-app-region:drag]"
            />
            <TitlebarSlot
              area="titleBar.left"
              className="pointer-events-auto absolute z-10 flex w-max items-center gap-2 [-webkit-app-region:no-drag]"
              style={{
                left: 'max(calc(var(--workspace-left, 0px) + 0.5rem), calc(var(--titlebar-controls-left, 14px) + 2 * var(--titlebar-control-size, 1.25rem) + 1rem))'
              }}
            />
            <TitlebarSlot
              area="titleBar.center"
              className="pointer-events-auto absolute left-1/2 top-1/2 z-10 flex w-max -translate-x-1/2 -translate-y-1/2 items-center gap-2 [-webkit-app-region:no-drag]"
            />
            <TitlebarSlot
              area="titleBar.right"
              className="pointer-events-auto absolute z-10 flex w-max items-center gap-2 [-webkit-app-region:no-drag]"
              style={{
                right:
                  'max(calc(var(--workspace-right, 0px) + 0.5rem), calc(var(--titlebar-tools-right, 0.75rem) + 4 * (var(--titlebar-control-size, 1.25rem) + 0.25rem) + 0.5rem))'
              }}
            />
          </div>

          <LayoutTreeRoot />

          {/* "Close running tab?" — the busy/input-blocked tile close gate. */}
          <SessionTileCloseConfirm />

          {/* The REAL statusbar (model pill, command center, agents, …) with
              statusBar.left/right contributions merged in. Unmounted — not
              just hidden — while toggled off, so its 15s status poll and the
              per-turn readouts stop with it. */}
          {statusbarVisible && <WiredPane part="statusbar" />}
        </div>
      </ContribWiring>
    </SidebarProvider>
  )
}

// Referenced type kept for plugin authors' reference (payload shape of
// statusBar.* contributions).
export type { StatusbarItem }
