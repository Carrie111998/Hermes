import type * as React from 'react'
import { useState } from 'react'

import { BRAND } from '@/lib/brand'
import { cn } from '@/lib/utils'

import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { PageSearchShell } from '../page-search-shell'

import { FoundrlyPortalChat } from './portal-chat-pane'

/**
 * Foundrly product workspace.
 *
 * Markers for check-brand-separation (do not rename lightly):
 *   - persist:foundrly-home
 *   - persist:foundrly-portal
 *   - Overnight visibility
 *   - Foundrly home
 *
 * Admin copilot is the live Intelliverse-X-Webfrontend portal at
 * /admin/portal/chat (OTP + scoped tools). Hermes left-rail chat stays
 * separate. Do not import Agency native copilot IPC.
 */
const WEB_URL = BRAND.foundrly?.webUrl ?? 'https://getfoundrly.com'
const ADMIN_URL = BRAND.foundrly?.adminPortalUrl ?? 'https://admin.intelli-verse-x.ai/admin/portal'

const FOUNDRLY_MODES = ['chat', 'home'] as const
type FoundrlyMode = (typeof FOUNDRLY_MODES)[number]

const TAB_LABEL: Record<FoundrlyMode, string> = {
  chat: 'Admin copilot',
  home: 'Home'
}

async function openExternal(url: string) {
  const bridge = window.hermesDesktop?.foundrly
  if (bridge?.openUrl) {
    await bridge.openUrl(url)
    return
  }
  window.open(url, '_blank', 'noopener,noreferrer')
}

function FoundrlyHome({ onOpenChat }: { onOpenChat: () => void }) {
  return (
    <div className="h-full min-h-0 overflow-auto bg-[#0B1220] text-[#E7ECF5]">
      <header className="border-b border-white/10 px-8 py-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <img
              alt=""
              className="h-10 w-10 rounded-xl border border-teal-500/30 object-cover"
              height={40}
              src="/foundrly/mark-512.png"
              width={40}
            />
            <div>
              <p className="text-xs font-semibold uppercase tracking-[0.18em] text-teal-300/90">
                Foundrly home
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight">{BRAND.productName}</h1>
            </div>
          </div>
          <a
            className="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/[0.03] px-3 py-1.5 text-xs font-semibold text-white/70 hover:border-teal-500/40 hover:text-white"
            href="https://intelli-verse-x.ai"
            onClick={event => {
              event.preventDefault()
              void openExternal('https://intelli-verse-x.ai')
            }}
            rel="noreferrer"
            target="_blank"
          >
            Built on Intelliverse
          </a>
        </div>
        <p className="mt-3 max-w-xl text-sm text-white/65">
          AI co-founder desktop — Hermes on the left rail, Foundrly product surfaces here. Admin
          copilot is the same portal chat operators use in the browser (email OTP, Foundrly-scoped
          tools). Overnight visibility and growth workflows land in this workspace.
        </p>
      </header>

      <div className="grid gap-4 p-8 md:grid-cols-2">
        <article className="rounded-2xl border border-white/10 bg-white/[0.03] p-5">
          <h2 className="text-base font-semibold">Overnight visibility</h2>
          <p className="mt-2 text-sm text-white/60">
            Run overnight scans, approve drafts, and review morning results from the Foundrly product
            stack. Use Admin copilot for portal tools; Hermes chat for local ops questions.
          </p>
          <button
            type="button"
            className="mt-4 rounded-lg bg-teal-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-teal-400"
            onClick={() => void openExternal(WEB_URL)}
          >
            Open Foundrly web
          </button>
        </article>

        <article className="rounded-2xl border border-white/10 bg-white/[0.03] p-5">
          <h2 className="text-base font-semibold">Admin portal (scoped)</h2>
          <p className="mt-2 text-sm text-white/60">
            Foundrly operators with a portal grant sign in here — scope pin keeps data on Foundrly
            only. Same copilot as /admin/portal/chat on the web frontend.
          </p>
          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              className="rounded-lg bg-teal-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-teal-400"
              onClick={onOpenChat}
            >
              Open Admin copilot
            </button>
            <button
              type="button"
              className="rounded-lg border border-white/20 px-3 py-2 text-sm font-semibold text-white hover:bg-white/5"
              onClick={() => void openExternal(ADMIN_URL)}
            >
              Open Foundrly admin
            </button>
          </div>
        </article>
      </div>
    </div>
  )
}

export function FoundrlyView(props: React.ComponentProps<'section'>) {
  const [mode, setMode] = useRouteEnumParam('tab', FOUNDRLY_MODES, 'chat')
  const [query, setQuery] = useState('')

  return (
    <PageSearchShell
      {...props}
      activeTab={mode}
      className={cn('bg-[#0B1220] text-[#E7ECF5]', props.className)}
      data-foundrly-workspace="persist:foundrly-home"
      onSearchChange={setQuery}
      onTabChange={id => {
        setMode(id as FoundrlyMode)
        setQuery('')
      }}
      searchHidden
      searchPlaceholder=""
      searchValue={query}
      tabs={FOUNDRLY_MODES.map(id => ({ id, label: TAB_LABEL[id] }))}
    >
      <div className={cn('h-full min-h-0', mode !== 'chat' && 'hidden')}>
        <FoundrlyPortalChat />
      </div>
      {mode === 'home' ? <FoundrlyHome onOpenChat={() => setMode('chat')} /> : null}
    </PageSearchShell>
  )
}
