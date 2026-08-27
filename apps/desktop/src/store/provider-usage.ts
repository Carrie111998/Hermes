import { atom } from 'nanostores'

import type { ProviderUsageSnapshot } from '@/types/hermes'

import { $gateway } from './gateway'

export interface ProviderUsageState {
  loading: boolean
  providers: ProviderUsageSnapshot[]
}

const EMPTY: ProviderUsageState = { loading: false, providers: [] }

/**
 * Subscription usage for every provider this machine is authenticated with —
 * Claude, Codex, Kimi, OpenRouter, Copilot, and anything a provider plugin
 * adds. One store for the whole window: the gauge is account-scoped, not
 * session-scoped, so every surface shows the same numbers.
 */
export const $providerUsage = atom<ProviderUsageState>(EMPTY)

// The backend caches per provider with its own TTL and serves
// stale-while-revalidate, so a repeated call is nearly free. This guard is
// only about not stacking concurrent round-trips when a panel is opened
// twice in a row.
let inFlight: null | Promise<void> = null

export async function refreshProviderUsage(options: { force?: boolean } = {}): Promise<void> {
  if (inFlight) {
    return inFlight
  }

  const gateway = $gateway.get()

  if (!gateway) {
    return
  }

  $providerUsage.set({ ...$providerUsage.get(), loading: true })

  inFlight = (async () => {
    try {
      const result = await gateway.request<{ providers?: ProviderUsageSnapshot[] }>('account.usage', {
        refresh: Boolean(options.force)
      })

      $providerUsage.set({ loading: false, providers: result?.providers ?? [] })
    } catch {
      // Fail-open, and keep whatever we already had: the panel showing
      // slightly old numbers beats it blanking on a dropped socket.
      $providerUsage.set({ ...$providerUsage.get(), loading: false })
    } finally {
      inFlight = null
    }
  })()

  return inFlight
}

/** Test seam — the store is module state shared by every mounted surface. */
export function _resetProviderUsageForTests(): void {
  inFlight = null
  $providerUsage.set(EMPTY)
}
