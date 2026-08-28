/**
 * Group chat config: reads `group_chat.*` from config.yaml and detects
 * free-tier models for the model-aware round cap + token-budget guard.
 *
 * The model-aware cap is the follow-up the maintainer flagged on #96726:
 * `GROUP_CHAT_MAX_ROUNDS = 3` is hardcoded in the new group-chat.ts, and this
 * module makes it config-driven + auto-detected from the active model.
 */

import { host } from '@hermes/plugin-sdk'

export interface GroupChatConfig {
  /** Paid models — default 3 (range: 1-hard_cap) */
  max_rounds: number
  /** Free models — null = unlimited up to hard_cap */
  max_rounds_free: number | null
  /** Hard ceiling even for free models */
  hard_cap: number
  /** Token budget per drive (chars, ~4 chars/token) — always enforced */
  token_budget: number
  /** Quality-tier allowlist for free-model detection */
  free_models: string[]
}

const DEFAULT_GROUP_CHAT_CONFIG: GroupChatConfig = {
  max_rounds: 3,
  max_rounds_free: null,
  hard_cap: 20,
  token_budget: 80000,
  free_models: []
}

/**
 * Read group_chat config from config.yaml with safe defaults.
 * Falls back to defaults if config unavailable.
 */
export async function getGroupChatConfig(): Promise<GroupChatConfig> {
  try {
    if (host && typeof host.request === 'function') {
      const res = await host.request<{ value?: Record<string, unknown> }>('config.get', {
        key: 'group_chat'
      })
      const cfg = res?.value ?? {}
      return {
        max_rounds: Number(
          cfg.max_rounds != null ? cfg.max_rounds : DEFAULT_GROUP_CHAT_CONFIG.max_rounds
        ),
        max_rounds_free:
          cfg.max_rounds_free != null
            ? (cfg.max_rounds_free as number | null)
            : DEFAULT_GROUP_CHAT_CONFIG.max_rounds_free,
        token_budget: Number(
          cfg.token_budget != null ? cfg.token_budget : DEFAULT_GROUP_CHAT_CONFIG.token_budget
        ),
        free_models: Array.isArray(cfg.free_models)
          ? (cfg.free_models as string[])
          : DEFAULT_GROUP_CHAT_CONFIG.free_models,
        hard_cap: Number(
          cfg.hard_cap != null ? cfg.hard_cap : DEFAULT_GROUP_CHAT_CONFIG.hard_cap
        )
      }
    }
  } catch {
    // Fallback to defaults if config unavailable
  }
  return { ...DEFAULT_GROUP_CHAT_CONFIG }
}

/**
 * Detect whether a model is free-tier.
 *
 * Two-tier detection:
 * 1. `:free` suffix (OpenRouter convention) — automatic
 * 2. `free_models[]` substring match — explicit quality allowlist
 *
 * The allowlist is the authority: a model matching `free_models[]` gets
 * unlimited rounds even without the `:free` suffix. A model with only the
 * `:free` suffix but not in the allowlist is treated as free (the suffix
 * is the OpenRouter signal); set `free_models: []` and rely on the suffix
 * alone, or add patterns to gate quality.
 */
export function isFreeModel(modelName: unknown, config: GroupChatConfig): boolean {
  const name = String(modelName || '').trim()
  if (!name) return false
  // OpenRouter :free suffix convention
  if (/:free$/i.test(name)) return true
  // Explicit quality-tier allowlist
  if (config.free_models.some(pattern => name.toLowerCase().includes(pattern.toLowerCase()))) return true
  return false
}

/**
 * Get the effective round cap for the current model.
 *
 * Free models: max_rounds_free (null = Infinity, clamped to hard_cap)
 * Paid models: max_rounds (default 3)
 */
export function getEffectiveRoundCap(modelName: unknown, config: GroupChatConfig): number {
  if (isFreeModel(modelName, config)) {
    const cap = config.max_rounds_free
    if (cap === null || cap === undefined || cap === Infinity) {
      return config.hard_cap
    }
    return Math.min(Number(cap), config.hard_cap)
  }
  return Math.min(Number(config.max_rounds), config.hard_cap)
}

/**
 * Get current model name from the gateway via model.options RPC.
 */
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

/**
 * Rough token estimate: ~4 chars/token. This is an order-of-magnitude
 * backstop, not a billing meter; real token counts from the provider
 * will differ.
 */
export function estimateTokens(text: unknown): number {
  return Math.ceil(String(text || '').length / 4)
}
