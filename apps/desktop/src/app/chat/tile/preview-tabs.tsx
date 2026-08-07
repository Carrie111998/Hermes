import { useStore } from '@nanostores/react'
import { useCallback, useState } from 'react'

import { $restartPreviewServer } from '@/app/contrib/panes'
import { ActionsContextMenu } from '@/components/ui/actions-menu'
import { FileTypeIcon } from '@/components/ui/file-type-icon'
import { PaneTab, paneTabCloseItems, PaneTabLabel, PaneTabStrip } from '@/components/ui/pane-tab'
import { $previewReloadRequest, type PreviewTarget } from '@/store/preview'

import { PreviewPane } from '../right-rail/preview-pane'

/**
 * EMBEDDED PREVIEW TABS — files opened from an embedded file tree (the
 * workspace surface or a session tile) shown as tabs between chat and tree.
 *
 * The tab bar REUSES the app's pane-tab primitives (`PaneTabStrip` / `PaneTab`
 * / `PaneTabLabel`) — the exact strip session tiles, preview tiles and zone
 * headers render — so a preview tab answers middle-click close, ⌘-click close,
 * the active underline and the right-click menu the same way every other tab
 * does. The right-click menu uses the shared `paneTabCloseItems` verbs
 * (close / others / to the right / all), so it stays identical to a zone tab's.
 */

/** Tab-list management for one embedded preview strip (dedupe by URL, activate
 *  on open, neighbor-fallback when the active tab closes). Shared by the
 *  workspace surface and every session tile so both behave identically. */
export function usePreviewTabs() {
  const [tabs, setTabs] = useState<PreviewTarget[]>([])
  const [activeUrl, setActiveUrl] = useState<string | null>(null)

  const open = useCallback((preview: PreviewTarget) => {
    setTabs(prev => (prev.some(tab => tab.url === preview.url) ? prev : [...prev, preview]))
    setActiveUrl(preview.url)
  }, [])

  const close = useCallback((url: string) => {
    setTabs(prev => prev.filter(tab => tab.url !== url))
    setActiveUrl(current => {
      if (current !== url) {return current}
      const remaining = tabs.filter(tab => tab.url !== url)

      return remaining[Math.min(tabs.findIndex(tab => tab.url === url), remaining.length - 1)]?.url ?? null
    })
  }, [tabs])

  const closeOthers = useCallback((url: string) => {
    setTabs(prev => prev.filter(tab => tab.url === url))
    setActiveUrl(url)
  }, [])

  const closeToRight = useCallback(
    (url: string) => {
      const idx = tabs.findIndex(tab => tab.url === url)

      if (idx < 0) {return}
      const kept = tabs.slice(0, idx + 1)
      setTabs(kept)
      setActiveUrl(current => (kept.some(tab => tab.url === current) ? current : kept[kept.length - 1]?.url ?? null))
    },
    [tabs]
  )

  const closeAll = useCallback(() => {
    setTabs([])
    setActiveUrl(null)
  }, [])

  return { activeUrl, close, closeAll, closeOthers, closeToRight, open, setActiveUrl, tabs }
}

interface PreviewTabsProps {
  activeUrl: string | null
  onActivate: (url: string) => void
  onClose: (url: string) => void
  onCloseAll: () => void
  onCloseOthers: (url: string) => void
  onCloseToRight: (url: string) => void
  /** Stable prefix for per-tab console keys (e.g. a storedSessionId). */
  scopeId: string
  tabs: PreviewTarget[]
}

function tabLabel(target: PreviewTarget): string {
  return (
    target.label ||
    target.path?.split(/[\\/]+/).filter(Boolean).pop() ||
    target.url ||
    'Preview'
  )
}

export function PreviewTabs({
  activeUrl,
  onActivate,
  onClose,
  onCloseAll,
  onCloseOthers,
  onCloseToRight,
  scopeId,
  tabs
}: PreviewTabsProps) {
  const previewReloadRequest = useStore($previewReloadRequest)
  const restartPreviewServer = useStore($restartPreviewServer)
  const activeTarget = tabs.find(tab => tab.url === activeUrl) ?? tabs[0] ?? null

  if (tabs.length === 0) {
    return null
  }

  return (
    <>
      <PaneTabStrip>
        {tabs.map((tab, idx) => {
          const active = tab.url === activeTarget?.url
          const label = tabLabel(tab)
          const counts = { all: tabs.length, others: tabs.length - 1, right: tabs.length - 1 - idx }

          return (
            <ActionsContextMenu
              contentClassName="w-40"
              items={kit =>
                paneTabCloseItems(kit, {
                  counts,
                  onClose: () => onClose(tab.url),
                  onCloseAll: () => onCloseAll(),
                  onCloseOthers: () => onCloseOthers(tab.url),
                  onCloseToRight: () => onCloseToRight(tab.url)
                })
              }
              key={tab.url}
            >
              <PaneTab
                active={active}
                className="cursor-pointer"
                onClick={() => onActivate(tab.url)}
                onClose={() => onClose(tab.url)}
                title={label}
              >
                <span className="grid size-4 shrink-0 place-items-center">
                  <FileTypeIcon className="opacity-70" path={tab.path || tab.url} size="0.6875rem" />
                </span>
                <PaneTabLabel className="normal-case tracking-normal">{label}</PaneTabLabel>
              </PaneTab>
            </ActionsContextMenu>
          )
        })}
      </PaneTabStrip>
      {activeTarget && (
        <div className="min-h-0 flex-1">
          <PreviewPane
            embedded
            onRestartServer={activeTarget.kind === 'url' ? (restartPreviewServer ?? undefined) : undefined}
            reloadRequest={previewReloadRequest}
            tabId={`${scopeId}:${activeTarget.url}`}
            target={activeTarget}
          />
        </div>
      )}
    </>
  )
}
