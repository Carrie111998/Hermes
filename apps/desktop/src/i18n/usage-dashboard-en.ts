import type { UsageDashboardTranslations } from './usage-dashboard-types'

export const usageDashboardEn: UsageDashboardTranslations = {
  eyebrow: 'USAGE // LOCAL LEDGER',
  title: 'Usage deck',
  subtitle: 'Read the burn at a glance. Trace every captured call when you need the wiring.',
  rangeAria: 'Usage range',
  days: count => `${count}d`,
  sync: 'Sync',
  syncing: 'Syncing…',
  generated: when => `generated ${when}`,
  partialData: 'partial meter data',
  unknown: 'unknown',
  loading: 'loading…',
  emptyDaily: 'No daily activity in this range.',
  status: {
    active: 'ledger online',
    empty: 'ledger armed · no captures',
    degraded: 'ledger degraded'
  },
  sources: {
    session: 'session insights',
    install: 'install-wide meter'
  },
  deckAria: 'Usage deck view',
  decks: { overview: 'Overview', routes: 'Routes', ledger: 'Call ledger' },
  deckHints: {
    overview: 'macro burn · token topology · source pressure',
    routes: 'provider × model × API mode',
    ledger: 'latest captured calls · full token vector'
  },
  sections: {
    burn: '01 // BURN FIELD',
    token: '02 // TOKEN TOPOLOGY',
    model: '03 // MODEL STACK',
    cost: 'COST // TRUTH LAYER',
    platform: 'SOURCE // TRAFFIC',
    sessions: 'HOT // SESSIONS',
    workload: 'WORKLOAD // SIGNALS',
    routes: 'ROUTE // MATRIX',
    ledger: 'CALL // LEDGER'
  },
  macro: {
    marketCost: 'Market equiv.',
    rangeEstimate: days => `${days}d session estimate`,
    capturedCost: 'Captured estimate',
    captureUnavailable: 'install meter unavailable',
    pricingCoverage: (priced, included, unpriced) => `${priced} priced · ${included} included · ${unpriced} unpriced`,
    tokens: 'Token volume',
    inputOutput: (input, output) => `${input} in · ${output} out`,
    calls: 'API calls',
    sessions: 'Sessions',
    range: days => `last ${days} days`,
    cacheLeverage: 'Cache leverage',
    cacheRead: tokens => `${tokens} cache-read tokens`
  },
  chart: {
    title: 'Burn field',
    description: 'Daily pressure with an independent cumulative trace. Focus a bar for its exact value.',
    metricAria: 'Burn field metric',
    cost: 'Cost',
    tokens: 'Tokens',
    calls: 'Sessions',
    periodTotal: days => `${days}-day total`,
    cumulativeTrace: 'dashed = cumulative',
    aria: (metric, days) => `Daily ${metric} across ${days} days`
  },
  token: {
    title: 'Token topology',
    description: 'The full traffic mix, including cache and reasoning lanes rather than input/output alone.',
    input: 'Uncached input',
    cacheRead: 'Cache read',
    cacheWrite: 'Cache write',
    output: 'Output',
    reasoning: 'Reasoning',
    reasoningShare: share => `${share} of output`,
    tokensShort: 'tokens'
  },
  cost: {
    title: 'Cost truth',
    description:
      'Session provider actuals stay separate from captured local estimates, included usage, and unpriced calls.',
    captureUnavailable: 'The installation-wide usage meter is unavailable. Session estimates above remain visible.',
    actual: 'Session provider actual',
    estimated: 'Captured local estimate',
    included: 'Included usage',
    unavailable: 'Price unavailable',
    capturedAllTime: 'installation captured estimate',
    cacheSavings: 'estimated cache savings',
    calls: count => `${count} calls`,
    rangeComparison: (cost, days) =>
      `${days}d session market-equivalent: ${cost}. This is a separate scope, not added to the captured total.`,
    estimatedValue: cost => `estimate ${cost}`
  },
  models: {
    title: 'Model pressure stack',
    description: 'Session-derived model traffic with cache efficiency, reasoning load, and honest cost state.',
    empty: 'No model traffic in this range.'
  },
  sort: {
    aria: 'Sort usage rows',
    cost: 'Cost',
    tokens: 'Tokens',
    calls: 'Calls',
    cache: 'Cache'
  },
  table: {
    model: 'Model',
    route: 'Provider / model',
    apiMode: 'API mode',
    calls: 'Calls',
    input: 'Input',
    cacheRead: 'Cache read',
    cacheWrite: 'Cache write',
    output: 'Output',
    reasoning: 'Reasoning',
    cost: 'Cost',
    inspect: 'Inspect',
    profile: 'Profile / platform',
    tokens: 'Tokens',
    time: 'Time'
  },
  costStatus: {
    actual: 'actual',
    estimated: 'estimated',
    included: 'included',
    unknown: 'unknown',
    unpriced: 'unpriced',
    unavailable: 'unavailable',
    mixed: 'mixed'
  },
  platform: {
    title: 'Source pressure',
    description: 'Where session traffic enters Hermes, ranked by token volume.',
    empty: 'No platform traffic in this range.'
  },
  activity: {
    title: 'Clock heat',
    aria: 'Session activity by hour of day',
    cell: (hour, sessions) => `${hour}:00 · ${sessions} sessions`,
    peak: (hour, sessions) => `Peak window ${hour}:00 · ${sessions} sessions`
  },
  sessions: {
    title: 'Session hotspots',
    description: 'The heaviest session records in the selected range.',
    empty: 'No session hotspots in this range.',
    labels: {
      longest: 'Longest session',
      messages: 'Most messages',
      tokens: 'Most tokens',
      tools: 'Most tool calls'
    },
    messages: count => `${count} messages`,
    tokens: count => `${count} tokens`,
    calls: count => `${count} calls`,
    duration: {
      seconds: count => `${count}s`,
      minutes: count => `${count}m`,
      hours: count => `${count}h`,
      days: count => `${count}d`
    }
  },
  workload: {
    title: 'Workload signals',
    description: 'High-frequency tools and skills that help explain the shape of the traffic.',
    skill: 'skill',
    tool: 'tool',
    empty: 'No tool or skill activity in this range.',
    disclaimer: 'Activity count is context, not per-tool token or cost attribution.'
  },
  footer: {
    sessionInsights: 'session-derived telemetry',
    installLedger: 'capture-derived telemetry',
    localData: 'local state only'
  },
  scope: {
    all: 'All time',
    month: 'This month'
  },
  routes: {
    title: 'Route matrix',
    description:
      'Installation-wide aggregation by provider, model, and API mode. Crosshair a route to trace its latest calls.',
    scopeAria: 'Route matrix scope',
    loadFailed: 'Route telemetry could not be loaded. Session analytics are unaffected.',
    visible: (visible, total) => `${visible}/${total} routes`,
    calls: count => `${count} calls`,
    tokens: count => `${count} tokens`,
    cost: cost => `${cost} estimated`,
    inspect: route => `Inspect recent calls for ${route}`,
    noMatch: 'No routes match these controls.',
    empty: 'No captured routes yet. Enable the usage meter and make a model call to populate this matrix.',
    disclaimer:
      'Route totals begin when the usage meter starts capturing. They may cover a different time span than session insights.'
  },
  filters: {
    searchAria: 'Search usage routes',
    searchRoutes: 'Search route…',
    searchLedgerAria: 'Search captured calls',
    searchLedger: 'Search ID, route, profile, platform…',
    provider: 'Provider filter',
    model: 'Model filter',
    apiMode: 'API mode filter',
    platform: 'Platform filter',
    profile: 'Profile filter',
    costStatus: 'Cost state filter',
    allProviders: 'All providers',
    allModels: 'All models',
    allModes: 'All API modes',
    allPlatforms: 'All platforms',
    allProfiles: 'All profiles',
    allCostStates: 'All cost states',
    clear: 'Clear filters'
  },
  ledger: {
    title: 'Captured call ledger',
    description:
      'The latest installation-wide calls. Filter hard, then open a row for the complete token and identity vector.',
    limitAria: 'Captured call limit',
    loadFailed: 'Captured calls could not be loaded. Session analytics are unaffected.',
    scopeNotice:
      'Month scope is applied to the newest captured window. Older in-month calls may be absent because the recent ledger is limit-based.',
    visible: (visible, total) => `${visible}/${total} calls visible`,
    filterCount: count => `${count} active filters`,
    sessionId: 'Session ID',
    turnId: 'Task ID',
    eventId: 'Event ID',
    costSource: 'Cost source',
    timestamp: 'Timestamp',
    noMatch: 'No captured calls match these filters.',
    empty: 'No captured calls yet. The ledger fills as enabled usage-meter events arrive.',
    disclaimer:
      'The ledger is installation-wide across profiles. Its limit applies before local filters, so old routes may not appear in the latest window.'
  },
  error: {
    title: 'Usage telemetry unavailable',
    description: 'Hermes could not read the local session ledger.',
    retry: 'Retry'
  }
}
