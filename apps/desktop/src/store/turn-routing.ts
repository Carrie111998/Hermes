import { atom } from 'nanostores'

export type RoutingMode = 'auto' | 'observe' | 'off'
export type RouteEventType = 'route.applied' | 'route.completed' | 'route.decided' | 'route.degraded'

export interface TurnRouteTarget {
  enabled?: boolean
  kind?: 'moa' | 'model'
  model?: string
  preset?: string
  provider?: string
}

export interface TurnRouteStatus {
  confidence?: number
  event: RouteEventType
  mode: string
  reasonCode: string
  route: string
  shouldApply: boolean
  source: string
  target: TurnRouteTarget
  turnId: string
  turnSequence: number
}

export interface RoutingCapabilityState {
  available: boolean
  error: null | string
  loading: boolean
  mode: RoutingMode
  profile: string
  version: number
}

export interface RoutingBudgetResponse {
  available_slots?: unknown
  committed_slots?: unknown
  cooldown_reason_code?: unknown
  cooldown_until_at?: unknown
  reserved_slots?: unknown
  scope?: unknown
  week_key?: unknown
  weekly_limit?: unknown
}

export interface RoutingBudgetStatus {
  availableSlots: number
  committedSlots: number
  cooldownReasonCode: null | string
  cooldownUntilAt: null | number
  reservedSlots: number
  scope: string
  weekKey: string
  weeklyLimit: number
}

export interface RoutingBudgetState {
  available: boolean
  error: null | string
  generation: number
  loading: boolean
  profile: string
  status?: RoutingBudgetStatus
}

export const $routingCapability = atom<RoutingCapabilityState>({
  available: false,
  error: null,
  loading: false,
  mode: 'off',
  profile: '',
  version: 0
})

export const $routingBudget = atom<RoutingBudgetState>({
  available: false,
  error: null,
  generation: 0,
  loading: false,
  profile: ''
})

export const $turnRoutes = atom<Record<string, TurnRouteStatus>>({})

let capabilityGeneration = 0
let budgetGeneration = 0

export function beginRoutingBudgetRequest(profile: string): number {
  const generation = ++budgetGeneration
  const current = $routingBudget.get()
  $routingBudget.set(
    current.profile === profile
      ? { ...current, error: null, generation, loading: true }
      : { available: false, error: null, generation, loading: true, profile }
  )
  return generation
}

export function acceptRoutingBudget(
  generation: number,
  profile: string,
  raw: RoutingBudgetResponse
): boolean {
  const current = $routingBudget.get()
  if (generation !== budgetGeneration || current.profile !== profile) {
    return false
  }

  $routingBudget.set({
    available: true,
    error: null,
    generation,
    loading: false,
    profile,
    status: {
      availableSlots: nonNegativeInteger(raw.available_slots),
      committedSlots: nonNegativeInteger(raw.committed_slots),
      cooldownReasonCode: typeof raw.cooldown_reason_code === 'string' ? raw.cooldown_reason_code : null,
      cooldownUntilAt:
        typeof raw.cooldown_until_at === 'number' && Number.isFinite(raw.cooldown_until_at)
          ? raw.cooldown_until_at
          : null,
      reservedSlots: nonNegativeInteger(raw.reserved_slots),
      scope: typeof raw.scope === 'string' ? raw.scope : 'grok',
      weekKey: typeof raw.week_key === 'string' ? raw.week_key : '',
      weeklyLimit: nonNegativeInteger(raw.weekly_limit)
    }
  })
  return true
}

export function rejectRoutingBudget(
  generation: number,
  profile: string,
  error: unknown,
  unavailable = false
): boolean {
  const current = $routingBudget.get()
  if (generation !== budgetGeneration || current.profile !== profile) {
    return false
  }

  $routingBudget.set({
    ...current,
    available: unavailable ? false : current.available,
    error: error instanceof Error ? error.message : String(error || 'Routing budget unavailable'),
    loading: false
  })
  return true
}

export function beginRoutingCapabilityRequest(profile: string): number {
  const generation = ++capabilityGeneration
  const current = $routingCapability.get()
  $routingCapability.set(
    current.profile === profile
      ? { ...current, error: null, loading: true }
      : { available: false, error: null, loading: true, mode: 'off', profile, version: 0 }
  )
  return generation
}

export function beginRoutingModeWrite(profile: string, mode: RoutingMode): { generation: number; previous: RoutingMode } {
  const previous = $routingCapability.get().mode
  const generation = beginRoutingCapabilityRequest(profile)
  $routingCapability.set({ ...$routingCapability.get(), mode })
  return { generation, previous }
}

export function acceptRoutingCapability(
  generation: number,
  profile: string,
  rawMode: unknown,
  rawVersion: unknown
): boolean {
  const current = $routingCapability.get()
  if (generation !== capabilityGeneration || current.profile !== profile) {
    return false
  }

  const mode = rawMode === 'observe' || rawMode === 'auto' ? rawMode : 'off'
  const version = Number.isInteger(rawVersion) ? Number(rawVersion) : 0
  $routingCapability.set({ available: version >= 1, error: null, loading: false, mode, profile, version })
  return true
}

export function rejectRoutingCapability(
  generation: number,
  profile: string,
  error: unknown,
  unavailable = false,
  rollbackMode?: RoutingMode
): boolean {
  const current = $routingCapability.get()
  if (generation !== capabilityGeneration || current.profile !== profile) {
    return false
  }

  $routingCapability.set({
    ...current,
    available: unavailable ? false : current.available,
    error: error instanceof Error ? error.message : String(error || 'Routing status unavailable'),
    loading: false,
    mode: rollbackMode ?? current.mode
  })
  return true
}

export function clearTurnRoutes(): void {
  $turnRoutes.set({})
}

export function ingestTurnRouteEvent(sessionId: string, type: string, raw: unknown): boolean {
  if (!sessionId || !isRouteEventType(type) || !raw || typeof raw !== 'object') {
    return false
  }

  const payload = raw as Record<string, unknown>
  const turnId = typeof payload.turn_id === 'string' ? payload.turn_id : ''
  if (!turnId) {
    return false
  }

  const current = $turnRoutes.get()[sessionId]
  const payloadTurnSequence =
    typeof payload.turn_sequence === 'number' && Number.isInteger(payload.turn_sequence) && payload.turn_sequence > 0
      ? payload.turn_sequence
      : 0
  const turnSequence =
    payloadTurnSequence > 0
      ? payloadTurnSequence
      : current?.turnId === turnId
        ? current.turnSequence
        : 0
  if (
    type === 'route.decided' &&
    current &&
    current.turnId !== turnId &&
    current.turnSequence > 0 &&
    (turnSequence === 0 || turnSequence <= current.turnSequence)
  ) {
    return false
  }
  if (type !== 'route.decided' && current?.turnId !== turnId) {
    return false
  }

  const target = payload.target && typeof payload.target === 'object' ? (payload.target as TurnRouteTarget) : {}
  const next: TurnRouteStatus = {
    confidence: typeof payload.confidence === 'number' ? payload.confidence : undefined,
    event: type,
    mode: typeof payload.mode === 'string' ? payload.mode : '',
    reasonCode:
      typeof payload.selection_reason_code === 'string'
        ? payload.selection_reason_code
        : typeof payload.reason_code === 'string'
          ? payload.reason_code
          : '',
    route: typeof payload.route === 'string' ? payload.route : '',
    shouldApply: payload.should_apply === true,
    source: typeof payload.source === 'string' ? payload.source : '',
    target,
    turnId,
    turnSequence
  }

  $turnRoutes.set({ ...$turnRoutes.get(), [sessionId]: next })
  return true
}

function isRouteEventType(type: string): type is RouteEventType {
  return type === 'route.decided' || type === 'route.applied' || type === 'route.completed' || type === 'route.degraded'
}

function nonNegativeInteger(value: unknown): number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : 0
}
