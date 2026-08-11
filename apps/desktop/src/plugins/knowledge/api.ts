/**
 * Knowledge data layer — ctx.rest to the bundled plugins/knowledge Python
 * router (/api/plugins/knowledge/*). Graph + list + read + search, cached by
 * React Query with a slow refetch so external edits to the wiki show up.
 */
import { atom, type PluginRestOptions, queryClient } from '@hermes/plugin-sdk'

import type { KnowledgeGraph, KnowledgePage, KnowledgePageMeta, KnowledgeSearchMatch } from './types'

type Rest = <T>(path: string, opts?: PluginRestOptions) => Promise<T>

let rest: null | Rest = null

/** Selected page path for the reader panel. */
export const $selectedPage = atom<string | null>(null)

export function bindApi(r: Rest): () => void {
  rest = r

  return () => {
    rest = null
  }
}

function call<T>(path: string, opts?: PluginRestOptions): Promise<T> {
  return rest ? rest<T>(path, opts) : Promise.reject(new Error('knowledge api not ready'))
}

// ── query keys ───────────────────────────────────────────────────────────────

export const GRAPH_KEY = ['knowledge', 'graph'] as const
export const LIST_KEY = ['knowledge', 'list'] as const
export const pageKey = (path: string) => ['knowledge', 'page', path] as const
export const searchKey = (q: string) => ['knowledge', 'search', q] as const

// ── reads ────────────────────────────────────────────────────────────────────

export const fetchGraph = () => call<KnowledgeGraph>('/graph')
export const fetchList = () => call<{ ok: boolean; pages: KnowledgePageMeta[] }>('/list')
export const fetchPage = (path: string) => call<KnowledgePage>(`/read?path=${encodeURIComponent(path)}`)
export const searchPages = (q: string) =>
  call<{ ok: boolean; matches: KnowledgeSearchMatch[] }>(`/search?q=${encodeURIComponent(q)}`)

/** Invalidate graph + list after external wiki edits (e.g. from the agent). */
export function invalidateKnowledge(): void {
  void queryClient.invalidateQueries({ queryKey: GRAPH_KEY })
  void queryClient.invalidateQueries({ queryKey: LIST_KEY })
}
