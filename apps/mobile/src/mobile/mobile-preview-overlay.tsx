import { useEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'

import { PreviewPane } from '@/app/chat/right-rail/preview-pane'
import { Codicon } from '@/components/ui/codicon'
import { normalizePreviewAddress } from '@/app/chat/right-rail/preview-browser-bar'
import type { RightRailTabId } from '@/store/layout'
import type { PreviewTab } from '@/store/preview'

import { canEmbedMobilePreviewUrl } from './mobile-preview-policy'

export interface MobilePreviewOverlayProps {
  activeTabId: null | RightRailTabId
  onClose: () => void
  onCloseTab: (tabId: RightRailTabId) => void
  onNavigate: (url: string) => void
  onNewBrowserTab: () => void
  onOpenExternal: (url: string) => void
  onSelectTab: (tabId: RightRailTabId) => void
  open: boolean
  tabs: readonly PreviewTab[]
}

function tabLabel(tab: PreviewTab): string {
  return tab.target.label || (tab.target.kind === 'url' ? 'Browser' : 'Preview')
}

const PREVIEW_ASPECTS = [
  { id: 'portrait', label: '9:16', ratio: '9 / 16' },
  { id: 'square', label: '1:1', ratio: '1 / 1' },
  { id: 'landscape', label: '16:9', ratio: '16 / 9' }
] as const

type PreviewAspect = typeof PREVIEW_ASPECTS[number]['id']

const clampZoom = (value: number) => Math.max(0.7, Math.min(1.5, Math.round(value * 100) / 100))

function MobileUrlPreview({
  aspectRatio,
  onNavigate,
  onOpenExternal,
  target,
  zoom,
}: {
  aspectRatio: string
  onNavigate: (url: string) => void
  onOpenExternal: (url: string) => void
  target: PreviewTab['target']
  zoom: number
}) {
  const [draft, setDraft] = useState(target.url)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    setDraft(target.url)
    setReloadKey(key => key + 1)
  }, [target.url])

  const inlineUrl = canEmbedMobilePreviewUrl(target.url) ? target.url : null
  const commit = () => {
    const normalized = normalizePreviewAddress(draft)
    if (normalized && canEmbedMobilePreviewUrl(normalized)) {
      onNavigate(normalized)
    }
  }

  return (
    <div className="mobile-preview-url flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="mobile-preview-browserbar">
        <button aria-label="Reload preview" onClick={() => setReloadKey(key => key + 1)} type="button">
          <Codicon name="refresh" size="1rem" />
        </button>
        <input
          aria-label="Address"
          autoCapitalize="none"
          autoCorrect="off"
          inputMode="url"
          onChange={event => setDraft(event.target.value)}
          onKeyDown={event => {
            if (event.key !== 'Enter') return
            event.preventDefault()
            commit()
            event.currentTarget.blur()
          }}
          spellCheck={false}
          value={draft}
        />
        {inlineUrl && (
          <button
            aria-label="Open in system browser"
            className="mobile-preview-open-external"
            onClick={() => onOpenExternal(inlineUrl)}
            type="button"
          >
            <Codicon name="link-external" size="1rem" />
            <span>Open</span>
          </button>
        )}
      </div>
      {inlineUrl ? (
        <div className="mobile-preview-viewport" style={{ '--mobile-preview-aspect': aspectRatio, '--mobile-preview-zoom': zoom } as CSSProperties}>
          <iframe
            className="mobile-preview-webpage"
            key={`${inlineUrl}:${reloadKey}`}
            referrerPolicy="no-referrer"
            sandbox="allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-same-origin allow-scripts"
            src={inlineUrl}
            title={target.label || 'Browser'}
          />
        </div>
      ) : (
        <div className="mobile-preview-unavailable">
          <Codicon name="globe" size="2rem" />
          <strong>Use a private HTTPS preview address</strong>
          <p>Hermes Mobile does not load insecure or special browser schemes inside the app.</p>
        </div>
      )}
    </div>
  )
}

/**
 * Full-screen phone home for Desktop preview tabs. URL tabs use a sandboxed,
 * HTTPS-only iframe; file, PDF, Markdown, image, and artifact tabs continue
 * through the shared Desktop preview renderer and its remote filesystem path.
 */
export function MobilePreviewOverlay({
  activeTabId,
  onClose,
  onCloseTab,
  onNavigate,
  onNewBrowserTab,
  onOpenExternal,
  onSelectTab,
  open,
  tabs,
}: MobilePreviewOverlayProps) {
  const closePreviewRef = useRef<HTMLButtonElement>(null)
  const [aspect, setAspect] = useState<PreviewAspect>('landscape')
  const [zoom, setZoom] = useState(1)
  const selectedAspect = PREVIEW_ASPECTS.find(item => item.id === aspect) ?? PREVIEW_ASPECTS[2]
  const activeTab = useMemo(
    () => tabs.find(tab => tab.id === activeTabId) ?? tabs[0] ?? null,
    [activeTabId, tabs],
  )

  useEffect(() => {
    if (open && activeTab) closePreviewRef.current?.focus()
  }, [activeTab?.id, open])

  useEffect(() => {
    if (!open) return

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      onClose()
    }

    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onClose, open])

  if (!open || !activeTab) return null

  return (
    <section aria-label="Preview" aria-modal="true" className="mobile-preview-overlay" role="dialog">
      <header className="mobile-preview-header">
        <nav aria-label="Preview tabs" className="mobile-preview-tabs" role="tablist">
          {tabs.map(tab => {
            const label = tabLabel(tab)
            const selected = tab.id === activeTab.id
            return (
              <div className="mobile-preview-tab" key={tab.id}>
                <button
                  aria-selected={selected}
                  onClick={() => onSelectTab(tab.id)}
                  role="tab"
                  type="button"
                >
                  {label}
                </button>
                <button aria-label={`Close ${label}`} onClick={() => onCloseTab(tab.id)} type="button">
                  <Codicon name="close" size="0.9rem" />
                </button>
              </div>
            )
          })}
          <button aria-label="New browser tab" className="mobile-preview-new-tab" onClick={onNewBrowserTab} type="button">
            <Codicon name="add" size="1.1rem" />
          </button>
        </nav>
        <button aria-label="Close preview" className="mobile-preview-close" onClick={onClose} ref={closePreviewRef} type="button">
          <Codicon name="close" size="1.2rem" />
        </button>
      </header>
      <div aria-label="Preview framing" className="mobile-preview-controls" role="toolbar">
        {PREVIEW_ASPECTS.map(item => (
          <button aria-pressed={item.id === aspect} key={item.id} onClick={() => setAspect(item.id)} type="button">
            {item.label}
          </button>
        ))}
        <span aria-live="polite">{Math.round(zoom * 100)}%</span>
        <button aria-label="Zoom preview out" onClick={() => setZoom(current => clampZoom(current - 0.1))} type="button">−</button>
        <button aria-label="Reset preview zoom" onClick={() => setZoom(1)} type="button">1:1</button>
        <button aria-label="Zoom preview in" onClick={() => setZoom(current => clampZoom(current + 0.1))} type="button">+</button>
      </div>
      <div className="mobile-preview-content" role="tabpanel">
        {activeTab.target.kind === 'url' ? (
          <MobileUrlPreview
            aspectRatio={selectedAspect.ratio}
            onNavigate={onNavigate}
            onOpenExternal={onOpenExternal}
            target={activeTab.target}
            zoom={zoom}
          />
        ) : (
          <PreviewPane embedded tabId={activeTab.id} target={activeTab.target} />
        )}
      </div>
    </section>
  )
}
