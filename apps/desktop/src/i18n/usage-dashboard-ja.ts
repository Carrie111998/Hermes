import type { UsageDashboardTranslations } from './usage-dashboard-types'

export const usageDashboardJa: UsageDashboardTranslations = {
  eyebrow: '使用量 // ローカル台帳',
  title: '使用量デッキ',
  subtitle: '消費をひと目で把握し、必要なときは捕捉済みの各コールまで追跡できます。',
  rangeAria: '使用量の期間',
  days: count => `${count}日`,
  sync: '同期',
  syncing: '同期中…',
  generated: when => `生成日時 ${when}`,
  partialData: 'メーターデータの一部が利用不可',
  unknown: '不明',
  loading: '読み込み中…',
  emptyDaily: 'この期間の日別アクティビティはありません。',
  status: { active: '台帳オンライン', empty: '台帳待機中 · 捕捉なし', degraded: '台帳の状態が不完全' },
  sources: { session: 'セッション分析', install: 'インストール全体メーター' },
  deckAria: '使用量デッキ表示',
  decks: { overview: '概要', routes: 'ルート', ledger: 'コール台帳' },
  deckHints: {
    overview: '全体消費 · トークン構成 · ソース負荷',
    routes: 'プロバイダー × モデル × API モード',
    ledger: '最新の捕捉コール · 完全なトークン内訳'
  },
  sections: {
    burn: '01 // 消費フィールド',
    token: '02 // トークン構成',
    model: '03 // モデルスタック',
    cost: 'コスト // 根拠レイヤー',
    platform: 'ソース // トラフィック',
    sessions: '高負荷 // セッション',
    workload: 'ワークロード // 信号',
    routes: 'ルート // マトリクス',
    ledger: 'コール // 台帳'
  },
  macro: {
    marketCost: '市場換算コスト',
    rangeEstimate: days => `${days}日間のセッション推定`,
    capturedCost: '捕捉済み推定',
    captureUnavailable: 'インストールメーター利用不可',
    pricingCoverage: (priced, included, unpriced) =>
      `価格設定 ${priced} 件 · 含有 ${included} 件 · 未価格 ${unpriced} 件`,
    tokens: 'トークン量',
    inputOutput: (input, output) => `入力 ${input} · 出力 ${output}`,
    calls: 'API コール',
    sessions: 'セッション',
    range: days => `過去 ${days} 日`,
    cacheLeverage: 'キャッシュ活用率',
    cacheRead: tokens => `キャッシュ読取 ${tokens} トークン`
  },
  chart: {
    title: '消費フィールド',
    description: '日別の負荷と独立した累積軌跡を表示します。棒にフォーカスすると正確な値を確認できます。',
    metricAria: '消費フィールドの指標',
    cost: 'コスト',
    tokens: 'トークン',
    calls: 'セッション',
    periodTotal: days => `${days}日間の合計`,
    cumulativeTrace: '破線 = 累積',
    aria: (metric, days) => `${days}日間の日別${metric}`
  },
  token: {
    title: 'トークントポロジー',
    description: '入出力だけでなく、キャッシュと推論を含むトラフィック全体の構成です。',
    input: '未キャッシュ入力',
    cacheRead: 'キャッシュ読取',
    cacheWrite: 'キャッシュ書込',
    output: '出力',
    reasoning: '推論',
    reasoningShare: share => `出力の ${share}`,
    tokensShort: 'トークン'
  },
  cost: {
    title: 'コストの根拠',
    description: 'セッションのプロバイダー実額を、捕捉したローカル推定、含有利用、未価格コールと分けて表示します。',
    captureUnavailable: 'インストール全体の使用量メーターは利用できません。上のセッション推定は引き続き表示されます。',
    actual: 'セッションのプロバイダー実額',
    estimated: '捕捉済みローカル推定',
    included: '含有利用',
    unavailable: '価格不明',
    capturedAllTime: 'インストール捕捉推定',
    cacheSavings: '推定キャッシュ節約額',
    calls: count => `${count} コール`,
    rangeComparison: (cost, days) =>
      `${days}日間のセッション市場換算は ${cost}。捕捉合計とは別の範囲で、加算されません。`,
    estimatedValue: cost => `推定 ${cost}`
  },
  models: {
    title: 'モデル負荷スタック',
    description: 'モデル別のセッショントラフィック、キャッシュ効率、推論負荷、コスト状態です。',
    empty: 'この期間のモデルトラフィックはありません。'
  },
  sort: { aria: '使用量行を並べ替え', cost: 'コスト', tokens: 'トークン', calls: 'コール', cache: 'キャッシュ' },
  table: {
    model: 'モデル',
    route: 'プロバイダー / モデル',
    apiMode: 'API モード',
    calls: 'コール',
    input: '入力',
    cacheRead: 'キャッシュ読取',
    cacheWrite: 'キャッシュ書込',
    output: '出力',
    reasoning: '推論',
    cost: 'コスト',
    inspect: '確認',
    profile: 'プロファイル / プラットフォーム',
    tokens: 'トークン',
    time: '時刻'
  },
  costStatus: {
    actual: '実額',
    estimated: '推定',
    included: '含有',
    unknown: '不明',
    unpriced: '未価格',
    unavailable: '不明',
    mixed: '混在'
  },
  platform: {
    title: 'ソース負荷',
    description: 'セッショントラフィックの流入元をトークン量順に表示します。',
    empty: 'この期間のプラットフォームトラフィックはありません。'
  },
  activity: {
    title: '時間帯ヒートマップ',
    aria: '時間帯別のセッションアクティビティ',
    cell: (hour, sessions) => `${hour}:00 · ${sessions} セッション`,
    peak: (hour, sessions) => `ピーク ${hour}:00 · ${sessions} セッション`
  },
  sessions: {
    title: '高負荷セッション',
    description: '選択期間で最も使用量の多いセッション記録です。',
    empty: 'この期間の高負荷セッションはありません。',
    labels: {
      longest: '最長セッション',
      messages: '最多メッセージ',
      tokens: '最多トークン',
      tools: '最多ツールコール'
    },
    messages: count => `${count} メッセージ`,
    tokens: count => `${count} トークン`,
    calls: count => `${count} コール`,
    duration: {
      seconds: count => `${count}秒`,
      minutes: count => `${count}分`,
      hours: count => `${count}時間`,
      days: count => `${count}日`
    }
  },
  workload: {
    title: 'ワークロード信号',
    description: '使用頻度の高いツールとスキルからトラフィックの形を読み解けます。',
    skill: 'スキル',
    tool: 'ツール',
    empty: 'この期間のツールまたはスキルのアクティビティはありません。',
    disclaimer: 'アクティビティ数は参考情報であり、ツール別のトークンやコスト帰属ではありません。'
  },
  footer: {
    sessionInsights: 'セッション由来テレメトリ',
    installLedger: '捕捉由来テレメトリ',
    localData: 'ローカルデータのみ'
  },
  scope: { all: '全期間', month: '今月' },
  routes: {
    title: 'ルートマトリクス',
    description: 'プロバイダー、モデル、API モード別のインストール全体集計です。照準ボタンで最新コールを追跡できます。',
    scopeAria: 'ルートマトリクスの範囲',
    loadFailed: 'ルートテレメトリを読み込めません。セッション分析には影響しません。',
    visible: (visible, total) => `${visible}/${total} ルート`,
    calls: count => `${count} コール`,
    tokens: count => `${count} トークン`,
    cost: cost => `推定 ${cost}`,
    inspect: route => `${route} の最近のコールを確認`,
    noMatch: '条件に一致するルートはありません。',
    empty: '捕捉済みルートはまだありません。使用量メーターを有効にしてモデルを呼び出してください。',
    disclaimer: 'ルート合計は使用量メーターが捕捉を開始した時点からです。セッション分析とは期間が異なる場合があります。'
  },
  filters: {
    searchAria: '使用量ルートを検索',
    searchRoutes: 'ルートを検索…',
    searchLedgerAria: '捕捉済みコールを検索',
    searchLedger: 'ID、ルート、プロファイル、プラットフォームを検索…',
    provider: 'プロバイダー絞り込み',
    model: 'モデル絞り込み',
    apiMode: 'API モード絞り込み',
    platform: 'プラットフォーム絞り込み',
    profile: 'プロファイル絞り込み',
    costStatus: 'コスト状態絞り込み',
    allProviders: 'すべてのプロバイダー',
    allModels: 'すべてのモデル',
    allModes: 'すべての API モード',
    allPlatforms: 'すべてのプラットフォーム',
    allProfiles: 'すべてのプロファイル',
    allCostStates: 'すべてのコスト状態',
    clear: '絞り込みを解除'
  },
  ledger: {
    title: '捕捉済みコール台帳',
    description: 'インストール全体の最新コールです。絞り込んで行を開くと、完全なトークンと識別情報を確認できます。',
    limitAria: '捕捉コール件数',
    loadFailed: '捕捉済みコールを読み込めません。セッション分析には影響しません。',
    scopeNotice:
      '今月の範囲は最新の捕捉ウィンドウに適用されます。最近の台帳は件数制限があるため、今月の古いコールが含まれない場合があります。',
    visible: (visible, total) => `${visible}/${total} コール表示`,
    filterCount: count => `${count} 件の絞り込み`,
    sessionId: 'セッション ID',
    turnId: 'タスク ID',
    eventId: 'イベント ID',
    costSource: 'コスト根拠',
    timestamp: 'タイムスタンプ',
    noMatch: '絞り込みに一致するコールはありません。',
    empty: '捕捉済みコールはまだありません。有効な使用量メーターイベントが届くと台帳に追加されます。',
    disclaimer:
      '台帳は全プロファイルを対象にします。件数制限はローカル絞り込みより先に適用されるため、古いルートは表示されない場合があります。'
  },
  error: {
    title: '使用量テレメトリを利用できません',
    description: 'Hermes はローカルのセッション台帳を読み込めませんでした。',
    retry: '再試行'
  }
}
