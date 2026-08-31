export interface PaletteQueryState {
  hasData: boolean
  isError: boolean
  isPending: boolean
}

export type PaletteSearchStatus = 'error' | 'loading' | undefined

/** Choose the honest empty-state status for a type-to-search request. */
export function resolvePaletteSearchStatus(
  enabled: boolean,
  queries: readonly PaletteQueryState[]
): PaletteSearchStatus {
  if (!enabled) {
    return undefined
  }

  if (queries.some(query => query.isError)) {
    return 'error'
  }

  if (queries.some(query => query.isPending && !query.hasData)) {
    return 'loading'
  }

  return undefined
}
