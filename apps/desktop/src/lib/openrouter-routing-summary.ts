export interface OpenRouterRoutingSummaryInput {
  selectedTag: string
  selectedProviderName: string
  quantization: string
  allowFallbacks: boolean
  blockedTags: readonly string[]
  blockedProviderNames: readonly string[]
}

export interface OpenRouterRoutingSummaryCopy {
  automatic: string
  selectedOnly: string
  selectedPrefer: string
  blockedOnly: string
  selectedPreferBlocked: string
  endpointWithQuantization: string
  blockedJoinTwo: string
  blockedJoinMany: string
  blockedListSeparator: string
}

type TemplateValues = Record<string, string>

const format = (template: string, values: TemplateValues): string =>
  template.replace(/\{(\w+)\}/g, (placeholder, key: string) => values[key] ?? placeholder)

const nonEmptyUnique = (values: readonly string[]): string[] => {
  const seen = new Set<string>()
  const result: string[] = []

  for (const value of values) {
    const normalized = value.trim()

    if (normalized && !seen.has(normalized)) {
      seen.add(normalized)
      result.push(normalized)
    }
  }

  return result
}

const formatBlockedList = (blocked: readonly string[], copy: OpenRouterRoutingSummaryCopy): string => {
  if (blocked.length <= 1) {
    return blocked[0] ?? ''
  }

  if (blocked.length === 2) {
    return format(copy.blockedJoinTwo, { first: blocked[0], second: blocked[1] })
  }

  return format(copy.blockedJoinMany, {
    items: blocked.slice(0, -1).join(copy.blockedListSeparator),
    last: blocked[blocked.length - 1]
  })
}

export function summarizeOpenRouterRoute(
  route: OpenRouterRoutingSummaryInput,
  copy: OpenRouterRoutingSummaryCopy
): string {
  const selectedTag = route.selectedTag.trim()
  const providerName = route.selectedProviderName.trim()
  const provider = providerName || selectedTag
  const quantization = route.quantization.trim()

  const endpoint = provider
    ? quantization
      ? format(copy.endpointWithQuantization, { provider, quantization })
      : provider
    : ''

  const blocked = formatBlockedList(
    nonEmptyUnique(route.blockedProviderNames.length ? route.blockedProviderNames : route.blockedTags),
    copy
  )

  if (endpoint && !route.allowFallbacks) {
    return format(copy.selectedOnly, { endpoint })
  }

  if (endpoint && blocked) {
    return format(copy.selectedPreferBlocked, { endpoint, blocked })
  }

  if (endpoint) {
    return format(copy.selectedPrefer, { endpoint })
  }

  if (blocked) {
    return format(copy.blockedOnly, { blocked })
  }

  return copy.automatic
}
