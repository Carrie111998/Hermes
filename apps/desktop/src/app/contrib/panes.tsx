/**
 * Real-data panes + composable bar items for the contrib root:
 *
 *  - `PreviewRailPane` — the REAL ChatPreviewRail; files-pane clicks feed it.
 *  - `FilesPane` — real file browser; activating a file opens it in preview.
 *  - Core statusbar items with LIVE store-backed labels, registered as DATA
 *    contributions (`area: 'statusBar.left' / 'statusBar.right'`, payload =
 *    StatusbarItem) — plugins add theirs through the identical call.
 */

import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'
import { atom } from 'nanostores'
import { useMemo, useState } from 'react'

import { RightSidebarPane } from '@/app/right-sidebar'
import { ReviewPane } from '@/app/right-sidebar/review'
import type { GroupSetter } from '@/app/shell/group-setter'
import type { StatusbarItem } from '@/app/shell/statusbar-controls'
import type { TitlebarTool } from '@/app/shell/titlebar-controls'
import { DecodeText } from '@/components/ui/decode-text'
import { ResponsiveTabs } from '@/components/ui/tab-dropdown'
import { ContribBoundary } from '@/contrib/react/boundary'
import { useContributions } from '@/contrib/react/use-contributions'
import { registry } from '@/contrib/registry'
import { getLogs } from '@/hermes'
import { sessionTitle } from '@/lib/chat-runtime'
import { normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { cn } from '@/lib/utils'
import { openPreview } from '@/store/preview'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $activeSessionId, $currentCwd, $selectedStoredSessionId, $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

// ---------------------------------------------------------------------------
// Logs — live agent-log tail. OPTIONAL chrome: not in any default layout,
// hidden until the ⌘K "Toggle logs" command opens it ($logsOpen).
//
// Session filter: log lines are already stamped with " [session_id]" by
// hermes_logging's record factory (set_session_context()), so filtering here
// is just passing that id through to GET /api/logs?session=... — no new
// backend plumbing beyond exposing the CLI's existing `--session` filter on
// the endpoint (#70828).
// ---------------------------------------------------------------------------

const LOGS_SESSION_FILTER_ALL = 'all'
const LOGS_SESSION_FILTER_CURRENT = 'current'

export function resolveLogsSessionQueryValue({
  activeSessionId,
  selectedStoredSessionId,
  resolvedFilter
}: {
  activeSessionId: null | string
  selectedStoredSessionId: null | string
  resolvedFilter: string
}): string | undefined {
  if (resolvedFilter === LOGS_SESSION_FILTER_ALL) {
    return undefined
  }

  if (resolvedFilter === LOGS_SESSION_FILTER_CURRENT) {
    return selectedStoredSessionId ?? activeSessionId ?? undefined
  }

  return resolvedFilter
}

export function logsSessionsForProfile(sessions: SessionInfo[], activeGatewayProfile: string): SessionInfo[] {
  const profile = normalizeProfileKey(activeGatewayProfile)

  return sessions.filter(session => normalizeProfileKey(session.profile) === profile)
}

export function LogsPane() {
  const activeSessionId = useStore($activeSessionId)
  const activeGatewayProfile = useStore($activeGatewayProfile)
  const selectedStoredSessionId = useStore($selectedStoredSessionId)
  const sessions = useStore($sessions)
  const [sessionFilter, setSessionFilter] = useState<string>(LOGS_SESSION_FILTER_ALL)

  const profileSessions = useMemo(
    () => logsSessionsForProfile(sessions, activeGatewayProfile),
    [activeGatewayProfile, sessions]
  )

  // A specific picked session id can go stale (session closed/deleted) —
  // fall back to "all" rather than silently filtering on a dead id forever.
  const resolvedFilter = useMemo(() => {
    if (sessionFilter === LOGS_SESSION_FILTER_ALL || sessionFilter === LOGS_SESSION_FILTER_CURRENT) {
      return sessionFilter
    }

    return profileSessions.some(session => session.id === sessionFilter)
      ? sessionFilter
      : LOGS_SESSION_FILTER_ALL
  }, [profileSessions, sessionFilter])

  const sessionQueryValue = resolveLogsSessionQueryValue({
    activeSessionId,
    selectedStoredSessionId,
    resolvedFilter
  })

  const { data, error } = useQuery({
    queryKey: ['contrib-logs-tail', sessionQueryValue],
    queryFn: () => getLogs({ lines: 300, session: sessionQueryValue }),
    refetchInterval: 5000
  })

  const tabs = useMemo(() => {
    const base = [
      { id: LOGS_SESSION_FILTER_ALL, label: 'All' },
      { id: LOGS_SESSION_FILTER_CURRENT, label: 'Current' }
    ]

    // Recent sessions beyond "current", most-recently-active first, capped
    // so the picker stays usable rather than listing every stored session.
    const recents = [...profileSessions]
      .sort((a, b) => (b.last_active || b.started_at || 0) - (a.last_active || a.started_at || 0))
      .filter(session => session.id !== (selectedStoredSessionId ?? activeSessionId))
      .slice(0, 8)
      .map(session => ({ id: session.id, label: sessionTitle(session) }))

    return [...base, ...recents]
  }, [activeSessionId, profileSessions, selectedStoredSessionId])

  if (error) {
    return <div className="p-3 text-xs text-(--ui-text-quaternary)">log unavailable: {String(error)}</div>
  }

  if (!data) {
    return (
      <div className="grid h-full place-items-center">
        <DecodeText className="text-(--ui-text-quaternary)" cursor prefix={1} text="LOGS" />
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center border-b border-(--ui-border-primary) px-2.5 py-1">
        <ResponsiveTabs align="end" onChange={setSessionFilter} tabs={tabs} value={resolvedFilter} />
      </div>
      {/* No further chrome of its own — the zone header (when the user
         summons it) is the pane's only other label. Just the tail. */}
      <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-2.5 font-mono text-[0.66rem] leading-relaxed text-(--ui-text-secondary)">
        {data.lines.length ? data.lines.join('\n') : 'no matching log lines'}
      </pre>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Preview — the real rail, fed by the files pane
// ---------------------------------------------------------------------------

/** Preview-server restart handler, provided by the wiring (usePreviewRouting).
 *  Atom-bridged: this module can't import contrib-wiring (it imports us). */
export const $restartPreviewServer = atom<((url: string, context?: string) => Promise<string>) | null>(null)

/** Open a file from the tree in the real preview pipeline. */
function previewFile(path: string) {
  void normalizeOrLocalPreviewTarget(path, $currentCwd.get() || undefined)
    .then(target => {
      if (target) {
        openPreview(target, 'file-browser')
      }
    })
    .catch(() => undefined)
}

// Layout fit for wrapped asides. Edge chrome (borders/shadows) is neutralized
// GLOBALLY by the tree's seam invariant (see LayoutTreeRoot) — only sizing
// and titlebar clearance are per-wrapper concerns.
const ZONE_CONTENT = 'h-full [&>aside]:h-full [&>aside]:w-full [&>aside]:pt-0'

export function FilesPane() {
  return (
    <div className={ZONE_CONTENT}>
      <RightSidebarPane onActivateFile={previewFile} onActivateFolder={previewFile} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Review — the real git diff pane (⌘G / $reviewOpen)
// ---------------------------------------------------------------------------

export function ReviewPaneContent() {
  const cwd = useStore($currentCwd)

  // Keyed by cwd like DesktopController so switching projects rebuilds the
  // diff state instead of showing the previous repo's files.
  return (
    <div className={cn(ZONE_CONTENT, 'flex min-h-0 flex-col [&>aside]:min-h-0 [&>aside]:flex-1')}>
      <ReviewPane key={cwd || 'no-cwd'} />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Statusbar composability: plugins contribute DATA items into
// `statusBar.left` / `statusBar.right`; the wiring feeds them into the REAL
// useStatusbarItems as extraLeftItems/extraRightItems. No core filler here —
// the real statusbar owns the core items (model pill, terminal toggle, …).
// ---------------------------------------------------------------------------

/** Collect statusbar contributions for one side. A `render()` contribution
 *  becomes a render-item (arbitrary stateful node); otherwise the declarative
 *  `data` payload is the StatusbarItem. */
export function useStatusbarContributions(side: 'left' | 'right'): StatusbarItem[] {
  const items = useContributions(`statusBar.${side}`)

  return items
    .map(c =>
      c.render
        ? ({
            id: c.id,
            render: () => (
              <ContribBoundary id={c.id} variant="chip">
                {c.render!()}
              </ContribBoundary>
            )
          } satisfies StatusbarItem)
        : (c.data as StatusbarItem)
    )
    .filter(Boolean)
}

/** Collect TitlebarTool data contributions for one side of the titlebar. */
export function useTitlebarToolContributions(side: 'left' | 'right'): TitlebarTool[] {
  const items = useContributions(`titleBar.tools.${side}`)

  return items.map(c => c.data as TitlebarTool).filter(Boolean)
}

/**
 * Bridge a page's `GroupSetter` extension point (SkillsView, MessagingView,
 * ChatPreviewRail, …) into the registry: each call replaces the group's items
 * as DATA contributions in `<prefix>.<side>`, so page-owned items flow through
 * the same pipe plugins use. Setting an empty list clears the group.
 */
export function registryGroupSetter<T>(prefix: string): GroupSetter<T> {
  const disposers = new Map<string, () => void>()

  return (id, items, side = 'right') => {
    const key = `${side}:${id}`

    disposers.get(key)?.()
    disposers.set(
      key,
      registry.registerMany(
        items.map((item, i) => ({
          id: `${id}-${i}`,
          area: `${prefix}.${side}`,
          source: 'core',
          order: 100 + i,
          data: item as object
        }))
      )
    )
  }
}

/** The app's page-facing setters — the same `GroupSetter` shape pages already
 *  take as props, backed by the registry instead of component state. */
export const setStatusbarItemGroup = registryGroupSetter<StatusbarItem>('statusBar')
export const setTitlebarToolGroup = registryGroupSetter<TitlebarTool>('titleBar.tools')
