/** Plugin-scoped i18n for office (Claw3d) — English + Japanese bundles. */
import { type PluginLocaleBundles, usePluginI18n } from '@hermes/plugin-sdk'
import { useMemo } from 'react'

type OfficeMessages = {
  nav: string
  open: string
  title: string
  subtitle: string
  notCloned: string
  notInstalled: string
  running: string
  stopped: string
  devServer: string
  adapter: string
  port: string
  gateway: string
  oauthUnsupported: string
  oauthUnsupportedBody: string
  portInUse: string
  error: string
  noError: string
  setup: string
  setupTitle: string
  start: string
  stop: string
  openBrowser: string
  refresh: string
  logs: string
  noLogs: string
  step: (step: number, total: number, title: string) => string
  setupDone: string
  setupFailed: string
  startFailed: string
  stopFailed: string
  statusTitle: string
  actions: string
}

const en: OfficeMessages = {
  nav: 'Office',
  open: 'Office: Open Hermes Office',
  title: 'Hermes Office (Claw3d)',
  subtitle: 'A visual 3D interface for Hermes — runs the hermes-office dev server and a gateway adapter on localhost.',
  notCloned: 'Not installed',
  notInstalled: 'Installed, dependencies pending',
  running: 'Running',
  stopped: 'Stopped',
  devServer: 'Dev server',
  adapter: 'Gateway adapter',
  port: 'Port',
  gateway: 'Gateway',
  oauthUnsupported: 'OAuth gateway',
  oauthUnsupportedBody:
    'Office requires a local or token-authenticated gateway connection. Switch the connection in Settings to enable it.',
  portInUse: 'Port in use by another process',
  error: 'Error',
  noError: 'No errors',
  setup: 'Install / Update',
  setupTitle: 'Installing Hermes Office…',
  start: 'Start Office',
  stop: 'Stop',
  openBrowser: 'Open in browser',
  refresh: 'Refresh',
  logs: 'Logs',
  noLogs: 'No logs yet.',
  step: (step, total, title) => `Step ${step} of ${total} — ${title}`,
  setupDone: 'Hermes Office installed. Start it from the Office page.',
  setupFailed: 'Setup failed',
  startFailed: 'Could not start Office',
  stopFailed: 'Could not stop Office',
  statusTitle: 'Status',
  actions: 'Actions'
}

const ja: OfficeMessages = {
  nav: 'オフィス',
  open: 'オフィス: Hermes Office を開く',
  title: 'Hermes Office (Claw3d)',
  subtitle:
    'Hermes 用の3Dビジュアルインターフェース — hermes-office 開発サーバーとゲートウェイアダプターを localhost で実行します。',
  notCloned: '未インストール',
  notInstalled: 'インストール済み（依存関係の準備中）',
  running: '実行中',
  stopped: '停止中',
  devServer: '開発サーバー',
  adapter: 'ゲートウェイアダプター',
  port: 'ポート',
  gateway: 'ゲートウェイ',
  oauthUnsupported: 'OAuth ゲートウェイ',
  oauthUnsupportedBody:
    'Office にはローカルまたはトークン認証のゲートウェイ接続が必要です。設定で接続を切り替えてください。',
  portInUse: '別のプロセスがポートを使用中',
  error: 'エラー',
  noError: 'エラーなし',
  setup: 'インストール / 更新',
  setupTitle: 'Hermes Office をインストール中…',
  start: 'Office を起動',
  stop: '停止',
  openBrowser: 'ブラウザで開く',
  refresh: '更新',
  logs: 'ログ',
  noLogs: 'ログはまだありません。',
  step: (step, total, title) => `ステップ ${step} / ${total} — ${title}`,
  setupDone: 'Hermes Office がインストールされました。Office ページから起動してください。',
  setupFailed: 'セットアップに失敗しました',
  startFailed: 'Office を起動できませんでした',
  stopFailed: 'Office を停止できませんでした',
  statusTitle: 'ステータス',
  actions: 'アクション'
}

export const OFFICE_LOCALES: PluginLocaleBundles = { en, ja }

export function useOfficeI18n(): OfficeMessages {
  const t = usePluginI18n('office')

  return useMemo(() => t as unknown as OfficeMessages, [t])
}
