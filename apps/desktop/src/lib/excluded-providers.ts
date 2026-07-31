import type { HermesConfigRecord } from '@/types/hermes'

/** `model_catalog.excluded_providers` — the backend's provider blocklist. Every
 *  picker surface (desktop, TUI, `hermes model`, gateway) builds its catalog
 *  through `load_picker_context()`, which drops these slugs, so a provider
 *  excluded here disappears everywhere instead of only in this app's UI.
 *
 *  Unlike per-model visibility (`store/model-visibility`, a localStorage
 *  presentation preference), this is real configuration: it survives to the CLI
 *  and keeps providers that self-authenticate from ambient credentials — a
 *  logged-in `gh` for Copilot, Claude Code's OAuth file — out of the picker. */
const CATALOG_BLOCK = 'model_catalog'
const EXCLUDED_KEY = 'excluded_providers'

function catalogBlock(config: HermesConfigRecord | undefined): Record<string, unknown> {
  const block = config?.[CATALOG_BLOCK]

  return typeof block === 'object' && block !== null && !Array.isArray(block) ? (block as Record<string, unknown>) : {}
}

/** Excluded slugs from a config record. A hand-written scalar (`excluded_providers:
 *  copilot`) reads as a one-entry list; anything unusable is dropped. */
export function readExcludedProviders(config: HermesConfigRecord | undefined): string[] {
  const raw = catalogBlock(config)[EXCLUDED_KEY]
  const entries = Array.isArray(raw) ? raw : raw === undefined || raw === null ? [] : [raw]

  return entries.filter((entry): entry is string => typeof entry === 'string' && entry.trim() !== '')
}

/** Case-insensitive membership, matching the backend's normalized comparison. */
export function isProviderExcluded(excluded: readonly string[], slug: string): boolean {
  const target = slug.toLowerCase()

  return excluded.some(entry => entry.toLowerCase() === target)
}

/** Next blocklist with one provider switched on/off. */
export function withProviderExcluded(excluded: readonly string[], slug: string, next: boolean): string[] {
  const target = slug.toLowerCase()
  const without = excluded.filter(entry => entry.toLowerCase() !== target)

  return next ? [...without, slug] : without
}

/** Config record with the blocklist replaced. The last provider being switched
 *  back on writes an explicit empty list rather than dropping the key: `PUT
 *  /api/config` deep-merges over what's on disk, so an absent key keeps the
 *  stored list and the provider would stay hidden. An empty list is inert to the
 *  backend. */
export function withExcludedProviders(config: HermesConfigRecord, excluded: readonly string[]): HermesConfigRecord {
  return { ...config, [CATALOG_BLOCK]: { ...catalogBlock(config), [EXCLUDED_KEY]: [...excluded] } }
}
