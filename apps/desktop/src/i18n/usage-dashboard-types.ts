export type UsageDashboardTranslations = {
  eyebrow: string
  title: string
  subtitle: string
  rangeAria: string
  days: (count: number) => string
  sync: string
  syncing: string
  generated: (when: string) => string
  partialData: string
  unknown: string
  loading: string
  emptyDaily: string
  status: {
    active: string
    empty: string
    degraded: string
  }
  sources: {
    session: string
    install: string
  }
  deckAria: string
  decks: Record<'overview' | 'routes' | 'ledger', string>
  deckHints: Record<'overview' | 'routes' | 'ledger', string>
  sections: Record<
    'burn' | 'token' | 'model' | 'cost' | 'platform' | 'sessions' | 'workload' | 'routes' | 'ledger',
    string
  >
  macro: {
    marketCost: string
    rangeEstimate: (days: number) => string
    capturedCost: string
    captureUnavailable: string
    pricingCoverage: (priced: number, included: number, unpriced: number) => string
    tokens: string
    inputOutput: (input: string, output: string) => string
    calls: string
    sessions: string
    range: (days: number) => string
    cacheLeverage: string
    cacheRead: (tokens: string) => string
  }
  chart: {
    title: string
    description: string
    metricAria: string
    cost: string
    tokens: string
    calls: string
    periodTotal: (days: number) => string
    cumulativeTrace: string
    aria: (metric: string, days: number) => string
  }
  token: {
    title: string
    description: string
    input: string
    cacheRead: string
    cacheWrite: string
    output: string
    reasoning: string
    reasoningShare: (share: string) => string
    tokensShort: string
  }
  cost: {
    title: string
    description: string
    captureUnavailable: string
    actual: string
    estimated: string
    included: string
    unavailable: string
    capturedAllTime: string
    cacheSavings: string
    calls: (count: string) => string
    rangeComparison: (cost: string, days: number) => string
    estimatedValue: (cost: string) => string
  }
  models: {
    title: string
    description: string
    empty: string
  }
  sort: {
    aria: string
    cost: string
    tokens: string
    calls: string
    cache: string
  }
  table: {
    model: string
    route: string
    apiMode: string
    calls: string
    input: string
    cacheRead: string
    cacheWrite: string
    output: string
    reasoning: string
    cost: string
    inspect: string
    profile: string
    tokens: string
    time: string
  }
  costStatus: Record<string, string>
  platform: {
    title: string
    description: string
    empty: string
  }
  activity: {
    title: string
    aria: string
    cell: (hour: number, sessions: number) => string
    peak: (hour: string, sessions: string) => string
  }
  sessions: {
    title: string
    description: string
    empty: string
    labels: Record<'longest' | 'messages' | 'tokens' | 'tools', string>
    messages: (count: string) => string
    tokens: (count: string) => string
    calls: (count: string) => string
    duration: {
      seconds: (count: string) => string
      minutes: (count: string) => string
      hours: (count: string) => string
      days: (count: string) => string
    }
  }
  workload: {
    title: string
    description: string
    skill: string
    tool: string
    empty: string
    disclaimer: string
  }
  footer: {
    sessionInsights: string
    installLedger: string
    localData: string
  }
  scope: {
    all: string
    month: string
  }
  routes: {
    title: string
    description: string
    scopeAria: string
    loadFailed: string
    visible: (visible: string, total: string) => string
    calls: (count: string) => string
    tokens: (count: string) => string
    cost: (cost: string) => string
    inspect: (route: string) => string
    noMatch: string
    empty: string
    disclaimer: string
  }
  filters: {
    searchAria: string
    searchRoutes: string
    searchLedgerAria: string
    searchLedger: string
    provider: string
    model: string
    apiMode: string
    platform: string
    profile: string
    costStatus: string
    allProviders: string
    allModels: string
    allModes: string
    allPlatforms: string
    allProfiles: string
    allCostStates: string
    clear: string
  }
  ledger: {
    title: string
    description: string
    limitAria: string
    loadFailed: string
    scopeNotice: string
    visible: (visible: string, total: string) => string
    filterCount: (count: string) => string
    sessionId: string
    turnId: string
    eventId: string
    costSource: string
    timestamp: string
    noMatch: string
    empty: string
    disclaimer: string
  }
  error: {
    title: string
    description: string
    retry: string
  }
}
