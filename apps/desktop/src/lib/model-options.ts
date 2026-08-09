import { getGlobalModelOptions, type HermesGateway, type ModelOptionsResponse } from '@/hermes'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import type { ModelOptionProvider } from '@/types/hermes'

/**
 * True only when a persisted **manual** composer pick has been removed from the
 * catalog (its provider still ships models, but no longer this one) — so a new
 * chat would keep 404'ing the dead model. Deliberately conservative to never
 * clobber a still-valid pick: an unknown/absent provider, an empty model list
 * (re-auth / unconfigured), or a not-yet-loaded catalog all return false.
 */
export function manualPickRemoved(
  providers: ModelOptionProvider[] | undefined,
  provider: string,
  model: string
): boolean {
  if (!providers?.length || !provider || !model) {
    return false
  }

  const row = providers.find(p => p.slug === provider || p.name === provider)

  if (!row) {
    return false
  }

  const models = row.models ?? []

  // Empty list means the provider is present but unconfigured / awaiting
  // re-auth, not that the model was dropped — leave the pick alone.
  if (models.length === 0) {
    return false
  }

  return !models.includes(model)
}

interface ModelOptionsRequest {
  /** When false, include ambient/unconfigured providers (onboarding/setup
   *  surfaces). Chat pickers default to true so only explicitly configured
   *  providers are listed (#56974). */
  explicitOnly?: boolean
  gateway?: HermesGateway
  refresh?: boolean
  sessionId?: null | string
}

export function modelOptionsQueryKey(profile: null | string | undefined, sessionId?: null | string) {
  const profileKey = (profile ?? '').trim() || 'default'

  return ['model-options', profileKey, sessionId || 'global'] as const
}

export function requestModelOptions({
  explicitOnly = true,
  gateway,
  refresh = false,
  sessionId
}: ModelOptionsRequest): Promise<ModelOptionsResponse> {
  // The catalog is scoped to the active gateway's profile; capture the identity
  // at REQUEST time so a late response resolving after a profile swap is still
  // keyed to the profile it was fetched for (see rememberProviders).
  const profile = normalizeProfileKey($activeGatewayProfile.get())

  if (gateway) {
    const params: Record<string, unknown> = {}

    if (sessionId) {
      params.session_id = sessionId
    }

    if (refresh) {
      params.refresh = true
    }

    if (explicitOnly) {
      params.explicit_only = true
    }

    return rememberProviders(gateway.request<ModelOptionsResponse>('model.options', params), profile)
  }

  return rememberProviders(getGlobalModelOptions({ explicitOnly, ...(refresh ? { refresh: true } : {}) }), profile)
}

// The composer-sync paths (session runtime state → sticky composer atoms) run
// outside React, where the queryClient cache is not reachable. Every catalog
// fetch (chat view, model picker, model menu) funnels through
// requestModelOptions, so mirror the last loaded provider list here for those
// synchronous reads. The mirror is read-only: the query cache stays
// authoritative for writes/refetches, and a stale mirror can at worst delay
// adopting a provider the user just re-enabled until the next catalog fetch.
//
// The mirror is keyed by the gateway profile the catalog belongs to (the query
// cache uses the same identity), so a response fetched for one profile can
// never drive adoptability decisions for another:
// `sessionProviderAdoptableFromCache` only consults the entry of the CURRENTLY
// active profile, and a late response from a previously active profile lands in
// that profile's own slot, inert.
let lastLoadedByProfile: Record<string, ModelOptionProvider[] | undefined> = {}

/** @internal Reset the mirror for tests. */
export function _resetLastLoadedProvidersForTests(): void {
  lastLoadedByProfile = {}
}

async function rememberProviders(
  response: ModelOptionsResponse | Promise<ModelOptionsResponse>,
  profile: string
): Promise<ModelOptionsResponse> {
  const resolved = await response

  lastLoadedByProfile[profile] = resolved?.providers

  return resolved
}

/**
 * Whether the composer may adopt `provider` from a resumed session's runtime
 * state.
 *
 * A session created under a provider whose credentials were later removed
 * (OAuth revoked, API key deleted) keeps stamping the dead provider on every
 * session.info heartbeat. Syncing it into the sticky composer state makes the
 * next send fail with a cryptic auth error ("No xAI OAuth credentials
 * stored...", "No Codex credentials stored..."). This guard blocks adoption
 * only when a LOADED catalog proves the provider has no usable credentials; an
 * unknown/not-yet-loaded catalog conservatively allows the sync — the same
 * philosophy as `manualPickRemoved`, so a still-valid pick is never clobbered
 * by a stale catalog.
 *
 * The chat catalog is fetched `explicitOnly`, so a loaded list contains only
 * configured providers plus the current provider's skeleton row (which carries
 * `authenticated: false` when it lost its credential). A present row with an
 * explicit `authenticated: false` or a provider absent from the loaded list is
 * therefore not usable right now.
 */
export function sessionProviderAdoptable(providers: ModelOptionProvider[] | undefined, provider: string): boolean {
  if (!provider || !Array.isArray(providers)) {
    return true
  }

  const row = providers.find(p => p.slug === provider || p.name === provider)

  return !row ? false : row.authenticated !== false
}

/** `sessionProviderAdoptable` against the current profile's last loaded catalog. */
export function sessionProviderAdoptableFromCache(provider: string): boolean {
  return sessionProviderAdoptable(lastLoadedByProfile[normalizeProfileKey($activeGatewayProfile.get())], provider)
}
