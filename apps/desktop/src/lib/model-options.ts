import { getGlobalModelOptions, type HermesGateway, type ModelOptionsResponse } from '@/hermes'
import type { ModelOptionProvider } from '@/types/hermes'

const MODEL_OPTIONS_CACHE_KEY = 'hermes.desktop.model-options.v1'
const MODEL_OPTIONS_CACHE_LIMIT = 12

export interface CachedModelOptions {
  data: ModelOptionsResponse
  updatedAt: number
}

type ModelOptionsCache = Record<string, CachedModelOptions>

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

function modelOptionsCacheEntryKey(profile: null | string | undefined, sessionId?: null | string): string {
  const profileKey = (profile ?? '').trim() || 'default'

  return `${profileKey}:${sessionId || 'global'}`
}

/**
 * Read the last successful catalog from the renderer's local cache. This is a
 * display cache only: React Query still revalidates stale entries in the
 * background, and no credentials are stored here.
 */
export function readModelOptionsCache(
  profile: null | string | undefined,
  sessionId?: null | string
): CachedModelOptions | undefined {
  try {
    const raw = localStorage.getItem(MODEL_OPTIONS_CACHE_KEY)

    if (!raw) {
      return undefined
    }

    const entry = (JSON.parse(raw) as ModelOptionsCache)[modelOptionsCacheEntryKey(profile, sessionId)]

    if (!entry || !Array.isArray(entry.data?.providers) || !Number.isFinite(entry.updatedAt)) {
      return undefined
    }

    return entry
  } catch {
    return undefined
  }
}

/** Persist a successful catalog so a cold picker open can render immediately. */
export function writeModelOptionsCache(
  profile: null | string | undefined,
  sessionId: null | string | undefined,
  data: ModelOptionsResponse
): void {
  try {
    const raw = localStorage.getItem(MODEL_OPTIONS_CACHE_KEY)
    const cache: ModelOptionsCache = raw ? (JSON.parse(raw) as ModelOptionsCache) : {}
    const key = modelOptionsCacheEntryKey(profile, sessionId)
    cache[key] = { data, updatedAt: Date.now() }

    const recent = Object.entries(cache)
      .sort(([, left], [, right]) => right.updatedAt - left.updatedAt)
      .slice(0, MODEL_OPTIONS_CACHE_LIMIT)

    localStorage.setItem(MODEL_OPTIONS_CACHE_KEY, JSON.stringify(Object.fromEntries(recent)))
  } catch {
    // localStorage can be unavailable or full; the in-memory React Query cache
    // remains the authoritative short-lived fallback.
  }
}

export function requestModelOptions({
  explicitOnly = true,
  gateway,
  refresh = false,
  sessionId
}: ModelOptionsRequest): Promise<ModelOptionsResponse> {
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

    return gateway.request<ModelOptionsResponse>('model.options', params)
  }

  return getGlobalModelOptions({ explicitOnly, ...(refresh ? { refresh: true } : {}) })
}
