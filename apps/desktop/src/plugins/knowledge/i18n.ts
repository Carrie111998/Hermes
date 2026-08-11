/** Plugin-scoped i18n for knowledge — English + Japanese bundles. */
import { type PluginLocaleBundles, usePluginI18n } from '@hermes/plugin-sdk'
import { useMemo } from 'react'

type KnowledgeMessages = {
  nav: string
  open: string
  title: string
  graph: string
  pages: string
  search: string
  searchPlaceholder: string
  noResults: string
  emptyTitle: string
  emptyBody: string
  stats: (nodes: number, edges: number) => string
  hoverDim: string
  legend: string
  backlinks: (n: number) => string
  links: (n: number) => string
  tags: string
  modified: string
  summary: string
  untitled: string
  refresh: string
  close: string
  notInstalled: string
  notInstalledBody: string
  openPage: string
}

const en: KnowledgeMessages = {
  nav: 'Knowledge',
  open: 'Knowledge: Open knowledge graph',
  title: 'Knowledge Graph',
  graph: 'Graph',
  pages: 'Pages',
  search: 'Search',
  searchPlaceholder: 'Search the wiki…',
  noResults: 'No matches',
  emptyTitle: 'No knowledge pages yet',
  emptyBody:
    'Drop Markdown files (with [[wiki-links]]) into your knowledge folder and they appear here as graph nodes. The folder was created at ~/.hermes/knowledge.',
  stats: (nodes, edges) => `${nodes} nodes · ${edges} edges`,
  hoverDim: 'Hover a node to highlight its connections',
  legend: 'Node types',
  backlinks: n => `${n} backlinks`,
  links: n => `${n} links`,
  tags: 'Tags',
  modified: 'Modified',
  summary: 'Summary',
  untitled: 'Untitled',
  refresh: 'Refresh',
  close: 'Close',
  notInstalled: 'Knowledge backend is not mounted',
  notInstalledBody:
    'Enable the knowledge plugin (Settings → Plugins) and restart the gateway, or run hermes with the dashboard server.',
  openPage: 'Open page'
}

const ja: KnowledgeMessages = {
  nav: 'ナレッジ',
  open: 'ナレッジ: ナレッジグラフを開く',
  title: 'ナレッジグラフ',
  graph: 'グラフ',
  pages: 'ページ',
  search: '検索',
  searchPlaceholder: 'ウィキを検索…',
  noResults: '一致なし',
  emptyTitle: 'ナレッジページがまだありません',
  emptyBody:
    '[[wikiリンク]]を含む Markdown ファイルをナレッジフォルダに置くと、グラフのノードとして表示されます。フォルダは ~/.hermes/knowledge に作成されました。',
  stats: (nodes, edges) => `${nodes} ノード · ${edges} エッジ`,
  hoverDim: 'ノードにホバーすると接続をハイライト',
  legend: 'ノードタイプ',
  backlinks: n => `${n} バックリンク`,
  links: n => `${n} リンク`,
  tags: 'タグ',
  modified: '更新',
  summary: '概要',
  untitled: '無題',
  refresh: '更新',
  close: '閉じる',
  notInstalled: 'ナレッジバックエンドがマウントされていません',
  notInstalledBody:
    'ナレッジプラグインを有効化（設定 → プラグイン）してゲートウェイを再起動するか、ダッシュボードサーバー付きで hermes を実行してください。',
  openPage: 'ページを開く'
}

export const KNOWLEDGE_LOCALES: PluginLocaleBundles = { en, ja }

export function useKnowledgeI18n(): KnowledgeMessages {
  const t = usePluginI18n('knowledge')

  return useMemo(() => t as unknown as KnowledgeMessages, [t])
}
