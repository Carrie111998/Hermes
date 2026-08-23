import { hermesApi, type ProfileScope, profileScoped } from './client'

export interface AccountUsageWindow {
  name: string
  used_percent?: number | null
  remaining_percent?: number | null
  reset_at?: null | string
  detail?: null | string
}

export interface AccountUsageProvider {
  provider: string
  status: string
  plan?: null | string
  windows?: AccountUsageWindow[]
  details?: string[]
  remaining_percent?: number | null
  reason?: string
}

export interface AccountUsageResponse {
  providers?: AccountUsageProvider[]
  unsupported?: AccountUsageProvider[]
  status?: string
  provider?: string
  reason?: string
  windows?: AccountUsageWindow[]
  details?: string[]
  plan?: null | string
}

export function getAccountUsage(profile?: ProfileScope, provider?: string): Promise<AccountUsageResponse> {
  const suffix = provider ? `?provider=${encodeURIComponent(provider)}` : ''
  return hermesApi<AccountUsageResponse>({
    ...profileScoped(profile),
    path: `/api/account-usage${suffix}`
  })
}

const LABELS: Record<string, string> = {
  'openai-codex': 'Codex',
  anthropic: 'Claude',
  openrouter: 'OR',
  nous: 'Nous'
}

export function formatQuotaChip(payload: AccountUsageResponse | null | undefined): {
  label: string
  tip: string
} {
  const rows = normalizeProviders(payload)
  if (!rows.length) return { label: '', tip: '' }
  const label = rows
    .map(row => {
      const remaining = tightestRemaining(row)
      const name = LABELS[row.provider] || row.provider
      return remaining == null ? `${name} —` : `${name} ${remaining}%`
    })
    .join(' · ')
  const tip = rows
    .map(row => {
      const name = LABELS[row.provider] || row.provider
      const bits = (row.windows || []).map(window => {
        const remaining = window.remaining_percent
        const reset = window.reset_at ? ` ${window.reset_at}` : ''
        return `${window.name} ${remaining ?? '—'}%${reset}`
      })
      return `${name}${row.plan ? ` ${row.plan}` : ''}: ${bits.join(' · ') || row.reason || 'ok'}`
    })
    .join('\n')
  return { label, tip }
}

function normalizeProviders(payload: AccountUsageResponse | null | undefined): AccountUsageProvider[] {
  if (!payload) return []
  if (Array.isArray(payload.providers)) {
    return payload.providers.filter(row => row.status === 'ok')
  }
  if (payload.status === 'ok' && payload.provider) {
    return [payload as AccountUsageProvider]
  }
  return []
}

function tightestRemaining(row: AccountUsageProvider): null | number {
  const values = (row.windows || [])
    .map(window => window.remaining_percent)
    .filter((value): value is number => typeof value === 'number')
  if (!values.length) return null
  return Math.min(...values)
}
