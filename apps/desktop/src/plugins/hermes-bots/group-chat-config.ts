/**
 * Group chat config: reads `group_chat.*` from config.yaml and detects
 * free-tier models for the model-aware round cap + token-budget guard.
 */

import { host } from '@hermes/plugin-sdk'

export interface GroupChatConfig {
  max_rounds: number
  max_rounds_free: number | null
  hard_cap: number
  token_budget: number
  free_models: string[]
}

const DEFAULT_GROUP_CHAT_CONFIG: GroupChatConfig = {
  max_rounds: 3,
  max_rounds_free: null,
  hard_cap: 20,
  token_budget: 80000,
  free_models: []
}

export async function getGroupChatConfig(): Promise<GroupChatConfig> {
  try {
    if (host && typeof host.request === 'function') {
      const res = await host.request<{ value?: Record<string, unknown> }>('config.get', {
        key: 'group_chat'
      })
      const cfg = res?.value ?? {}
      return {
        max_rounds: Number(cfg.max_rounds != null ? cfg.max_rounds : DEFAULT_GROUP_CHAT_CONFIG.max_rounds),
        max_rounds_free: cfg.max_rounds_free != null ? (cfg.max_rounds_free as number | null) : DEFAULT_GROUP_CHAT_CONFIG.max_rounds_free,
        token_budget: Number(cfg.token_budget != null ? cfg.token_budget : DEFAULT_GROUP_CHAT_CONFIG.token_budget),
        free_models: Array.isArray(cfg.free_models) ? (cfg.free_models as string[]) : DEFAULT_GROUP_CHAT_CONFIG.free_models,
        hard_cap: Number(cfg.hard_cap != null ? cfg.hard_cap : DEFAULT_GROUP_CHAT_CONFIG.hard_cap)
      }
    }
  } catch {
    // Fallback to defaults
  }
  return { ...DEFAULT_GROUP_CHAT_CONFIG }
}

export function isFreeModel(modelName: unknown, config: GroupChatConfig): boolean {
  const name = String(modelName || '').trim()
  if (!name) return false
  if (/:free$/i.test(name)) return true
  if (config.free_models.some(pattern => name.toLowerCase().includes(pattern.toLowerCase()))) return true
  return false
}

export function getEffectiveRoundCap(modelName: unknown, config: GroupChatConfig): number {
  if (isFreeModel(modelName, config)) {
    const cap = config.max_rounds_free
    if (cap === null || cap === undefined || cap === Infinity) return config.hard_cap
    return Math.min(Number(cap), config.hard_cap)
  }
  return Math.min(Number(config.max_rounds), config.hard_cap)
}

export async function getCurrentModelName(): Promise<string> {
  try {
    if (host && typeof host.request === 'function') {
      const res = await host.request<{ model?: string }>('model.options', {})
      if (res?.model) return String(res.model).trim()
    }
  } catch {
    // Ignore
  }
  return ''
}

export function estimateTokens(text: unknown): number {
  return Math.ceil(String(text || '').length / 4)
}
