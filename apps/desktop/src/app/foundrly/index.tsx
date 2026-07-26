import type * as React from 'react'

import { BRAND } from '@/lib/brand'

/**
 * Foundrly product workspace (MVP).
 *
 * Markers for check-brand-separation (do not rename lightly):
 *   - persist:foundrly-home
 *   - Overnight visibility
 *   - Foundrly home
 */
const WEB_URL = BRAND.foundrly?.webUrl ?? 'https://foundrly.intelli-verse-x.ai'
const ADMIN_URL = BRAND.foundrly?.adminPortalUrl ?? 'https://admin.intelli-verse-x.ai/admin/portal'

async function openExternal(url: string) {
  const bridge = window.hermesDesktop?.foundrly
  if (bridge?.openUrl) {
    await bridge.openUrl(url)
    return
  }
  window.open(url, '_blank', 'noopener,noreferrer')
}

export function FoundrlyView(props: React.ComponentProps<'section'>) {
  return (
    <section
      {...props}
      className="flex h-full min-h-0 flex-col overflow-auto bg-[#0B1220] text-[#E7ECF5]"
      data-foundrly-workspace="persist:foundrly-home"
    >
      <header className="border-b border-white/10 px-8 py-6">
        <p className="text-xs font-semibold uppercase tracking-[0.18em] text-teal-300/90">Foundrly home</p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">{BRAND.productName}</h1>
        <p className="mt-2 max-w-xl text-sm text-white/65">
          AI co-founder desktop for local businesses — Hermes chat on the left rail, Foundrly product
          surfaces here. Overnight visibility and growth workflows land in this workspace.
        </p>
      </header>

      <div className="grid gap-4 p-8 md:grid-cols-2">
        <article className="rounded-2xl border border-white/10 bg-white/[0.03] p-5">
          <h2 className="text-base font-semibold">Overnight visibility</h2>
          <p className="mt-2 text-sm text-white/60">
            Run overnight scans, approve drafts, and review morning results from the Foundrly product
            stack. Use Hermes chat for ops questions scoped to this App-ID.
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
            Foundrly operators with a portal grant sign in at the shared admin host — scope pin keeps
            data on Foundrly only.
          </p>
          <button
            type="button"
            className="mt-4 rounded-lg border border-white/20 px-3 py-2 text-sm font-semibold text-white hover:bg-white/5"
            onClick={() => void openExternal(ADMIN_URL)}
          >
            Open Foundrly admin
          </button>
        </article>
      </div>
    </section>
  )
}
