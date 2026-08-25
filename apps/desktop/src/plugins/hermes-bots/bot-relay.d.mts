export const RELAY_GLOBAL_CONCURRENCY: number
export const RELAY_TARGET_CONCURRENCY: number
export const RELAY_ROSTER_INTERVAL_MS: number
export const RELAY_DRAIN_INTERVAL_MS: number
export const RELAY_LEASE_SECONDS: number
export const RELAY_RENEW_INTERVAL_MS: number
export const RELAY_DELIVERY_REQUEST_TIMEOUT_MS: number
export const RELAY_LEADERSHIP_RETRY_MS: number

export interface BotRelayWorker {
  courierId: string
  courierNamespaceId: string
  drainOnce(): Promise<void>
  start(): void
  stop(): Promise<void>
  syncRosters(): Promise<void>
}

export function createRelayRuntimeId(prefix?: string): string
export function isMethodNotFound(error: unknown): boolean
export function retryDelaySeconds(envelope: unknown): number
export function relayTargetKey(envelope: unknown): string
export function createBotRelayWorker(options: object): BotRelayWorker
export interface BotRelaySupervisor {
  start(): void
  stop(): Promise<void>
}
export function createBotRelaySupervisor(options: object): BotRelaySupervisor
