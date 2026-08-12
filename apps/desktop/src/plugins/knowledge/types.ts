/** Knowledge graph data model, mirrored from plugins/knowledge/dashboard/plugin_api.py. */

export interface KnowledgeNode {
  id: string
  title: string
  type?: string | null
  tags: string[]
}

export interface KnowledgeEdge {
  source: string
  target: string
}

export interface KnowledgeGraph {
  ok: boolean
  root: string
  nodes: KnowledgeNode[]
  edges: KnowledgeEdge[]
}

export interface KnowledgePageMeta {
  path: string
  name: string
  title: string
  type?: string | null
  domain?: string | null
  status?: string | null
  tags: string[]
  summary?: string | null
  created?: string | null
  updated?: string | null
  size: number
  modified: string
  wikilinks: string[]
}

export interface KnowledgePage {
  ok: boolean
  meta: KnowledgePageMeta
  content: string
  backlinks: string[]
}

export interface KnowledgeSearchMatch {
  path: string
  title: string
  line: number
  text: string
}
