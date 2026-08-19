import type { HermesConfigRecord, OpenRouterEndpoint } from '@/hermes'

export interface OpenRouterRoutingDraft {
  allowFallbacks: boolean
  blockedTags: string[]
  providerTag: string
  quantization: string
}

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {}

export const normalizeOpenRouterTag = (value: unknown): string => (typeof value === 'string' ? value.trim() : '')

// The single shared predicate for "does this provider slug mean OpenRouter
// routing controls apply". Comparison sites must call this rather than
// re-deriving their own exact/lowercased match — an exact case-sensitive
// `=== 'openrouter'` silently hides the route picker + typeahead whenever a
// provider row reports a differently-cased slug (e.g. a stale/legacy
// `OpenRouter` display value), which is exactly what a scattered comparison
// invites.
//
// A `custom:`-prefixed slug is a user-defined OpenAI-compatible endpoint —
// even one whose base_url happens to be openrouter.ai does NOT count here.
// The backend's provider_routing resolution (agent_init.py
// _resolve_openrouter_provider_routing) gates on the literal provider being
// "openrouter"; a custom:openrouter request never receives only/ignore/
// order/quantizations/allow_fallbacks server-side. Showing these controls
// for it would let a user configure a lock that silently never applies.
export function isOpenRouterProvider(slug: string | null | undefined): boolean {
  return (slug ?? '').trim().toLowerCase() === 'openrouter'
}

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? [...new Set(value.map(normalizeOpenRouterTag).filter(Boolean))] : []

const firstString = (value: unknown): string => strings(value)[0] ?? ''

export function openRouterRoutingDraft(config: HermesConfigRecord | null, model: string): OpenRouterRoutingDraft {
  const routing = asRecord(config?.provider_routing)
  const overrides = asRecord(routing.model_overrides)
  const openrouter = asRecord(overrides.openrouter)
  const override = asRecord(openrouter[model])
  const only = firstString(override.only)
  const order = firstString(override.order)

  return {
    allowFallbacks: !!order && override.allow_fallbacks !== false,
    blockedTags: strings(override.ignore),
    providerTag: only || order,
    quantization: firstString(override.quantizations)
  }
}

export function updateOpenRouterRoutingConfig(
  config: HermesConfigRecord,
  model: string,
  draft: OpenRouterRoutingDraft
): HermesConfigRecord {
  const routing = { ...asRecord(config.provider_routing) }
  const overrides = { ...asRecord(routing.model_overrides) }
  const openrouter = { ...asRecord(overrides.openrouter) }
  const tag = normalizeOpenRouterTag(draft.providerTag)
  const quantization = draft.quantization.trim()

  const blockedTags = strings(draft.blockedTags).filter(item => item !== tag)

  if (!tag && blockedTags.length === 0) {
    delete openrouter[model]
  } else {
    openrouter[model] = {
      ...(tag
        ? draft.allowFallbacks
          ? { order: [tag], allow_fallbacks: true }
          : { only: [tag], allow_fallbacks: false }
        : {}),
      ...(tag && quantization ? { quantizations: [quantization] } : {}),
      ...(blockedTags.length ? { ignore: blockedTags } : {})
    }
  }

  overrides.openrouter = openrouter
  routing.model_overrides = overrides

  return { ...config, provider_routing: routing }
}

export function openRouterEndpointValue(endpoint: OpenRouterEndpoint): string {
  return JSON.stringify([normalizeOpenRouterTag(endpoint.tag), endpoint.quantization?.trim() ?? ''])
}

export function parseOpenRouterEndpointValue(
  value: string
): Pick<OpenRouterRoutingDraft, 'providerTag' | 'quantization'> {
  try {
    const [providerTag, quantization] = JSON.parse(value) as unknown[]

    return {
      providerTag: normalizeOpenRouterTag(providerTag),
      quantization: typeof quantization === 'string' ? quantization.trim() : ''
    }
  } catch {
    return { providerTag: '', quantization: '' }
  }
}
