import { useEffect, useRef, useState } from 'react'

import { BRAND } from '@/lib/brand'

/**
 * Guest partition for the live admin portal copilot.
 * Marker for check-brand-separation — do not rename lightly:
 *   persist:foundrly-portal
 */
export const FOUNDRLY_PORTAL_PARTITION = 'persist:foundrly-portal'

const ADMIN_CHAT_URL =
  BRAND.foundrly?.adminChatUrl ?? 'https://admin.intelli-verse-x.ai/admin/portal/chat'
const ADMIN_PORTAL_URL =
  BRAND.foundrly?.adminPortalUrl ?? 'https://admin.intelli-verse-x.ai/admin/portal'

type PortalWebview = HTMLElement & {
  getURL?: () => string
  loadURL?: (url: string) => void
  reload?: () => void
}

async function openExternal(url: string) {
  const bridge = window.hermesDesktop?.foundrly
  if (bridge?.openUrl) {
    await bridge.openUrl(url)
    return
  }
  window.open(url, '_blank', 'noopener,noreferrer')
}

function isPortalHost(url: string): boolean {
  try {
    const host = new URL(url).hostname
    return host === 'admin.intelli-verse-x.ai' || host.endsWith('.intelli-verse-x.ai')
  } catch {
    return false
  }
}

/**
 * Same AdminCopilotChat operators use in Intelliverse-X-Webfrontend at
 * /admin/portal/chat — OTP login, Foundrly-scoped tools, brand starters.
 * Isolated from Hermes left-rail chat and from Agency native copilot IPC.
 */
export function FoundrlyPortalChat() {
  const hostRef = useRef<HTMLDivElement>(null)
  const webviewRef = useRef<PortalWebview | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) {
      return
    }

    host.replaceChildren()
    const webview = document.createElement('webview') as PortalWebview
    webview.className = 'h-full w-full flex-1 bg-white'
    webview.setAttribute('partition', FOUNDRLY_PORTAL_PARTITION)
    webview.setAttribute('src', ADMIN_CHAT_URL)
    webview.setAttribute('allowpopups', '')
    webview.setAttribute('webpreferences', 'contextIsolation=yes,nodeIntegration=no,sandbox=yes')

    const onStart = () => {
      setLoadError(null)
      setLoading(true)
    }
    const onStop = () => setLoading(false)
    const onFail = (event: Event) => {
      const detail = event as Event & {
        errorCode?: number
        errorDescription?: string
        validatedURL?: string
      }
      if (detail.errorCode === -3) {
        return
      }
      setLoading(false)
      setLoadError(detail.errorDescription || detail.validatedURL || 'Failed to load admin copilot')
    }

    webview.addEventListener('did-start-loading', onStart)
    webview.addEventListener('did-stop-loading', onStop)
    webview.addEventListener('did-fail-load', onFail)
    host.appendChild(webview)
    webviewRef.current = webview

    return () => {
      webview.removeEventListener('did-start-loading', onStart)
      webview.removeEventListener('did-stop-loading', onStop)
      webview.removeEventListener('did-fail-load', onFail)
      webview.remove()
      webviewRef.current = null
    }
  }, [])

  return (
    <div className="flex h-full min-h-0 flex-col bg-[#0B1220]">
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-white/10 px-4 py-2">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-[0.16em] text-teal-300/90">
            Admin copilot
          </p>
          <p className="truncate text-xs text-white/55">
            Same portal chat as foundrly.intelli-verse-x.ai operators — email OTP, then Foundrly tools.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            className="rounded-lg border border-white/15 px-2.5 py-1 text-xs font-semibold text-white/80 hover:bg-white/5"
            type="button"
            onClick={() => webviewRef.current?.reload?.()}
          >
            Reload
          </button>
          <button
            className="rounded-lg border border-white/15 px-2.5 py-1 text-xs font-semibold text-white/80 hover:bg-white/5"
            type="button"
            onClick={() => void openExternal(ADMIN_CHAT_URL)}
          >
            Open in browser
          </button>
          <button
            className="rounded-lg border border-white/15 px-2.5 py-1 text-xs font-semibold text-white/80 hover:bg-white/5"
            type="button"
            onClick={() => void openExternal(ADMIN_PORTAL_URL)}
          >
            Tools grid
          </button>
        </div>
      </div>
      <div className="relative min-h-0 flex-1">
        <div
          ref={hostRef}
          className="absolute inset-0"
          data-foundrly-portal={FOUNDRLY_PORTAL_PARTITION}
          data-portal-src={ADMIN_CHAT_URL}
        />
        {loading ? (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center bg-[#0B1220]/40 text-sm text-white/70">
            Loading admin copilot…
          </div>
        ) : null}
        {loadError ? (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-[#0B1220] px-6 text-center">
            <p className="text-sm text-white/80">{loadError}</p>
            <button
              className="rounded-lg bg-teal-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-teal-400"
              type="button"
              onClick={() => {
                setLoadError(null)
                const current = webviewRef.current
                if (current?.loadURL) {
                  current.loadURL(ADMIN_CHAT_URL)
                  return
                }
                current?.setAttribute('src', ADMIN_CHAT_URL)
              }}
            >
              Retry
            </button>
          </div>
        ) : null}
      </div>
    </div>
  )
}

export { isPortalHost }
