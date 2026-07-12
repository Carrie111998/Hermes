import { queryClient, writeCache } from '@/lib/query-client'
import type { HermesConfigRecord } from '@/types/hermes'

// Shared cache identity for the effective config record of the routed profile.
// Keeping the writer outside React lets stores update it after config-backed
// keybinds and context-menu actions, not only after Settings form saves.
export const HERMES_CONFIG_KEY = ['hermes-config-record'] as const

export const setHermesConfigCache = writeCache<HermesConfigRecord>(HERMES_CONFIG_KEY)

export const invalidateHermesConfig = () => queryClient.invalidateQueries({ queryKey: HERMES_CONFIG_KEY })
