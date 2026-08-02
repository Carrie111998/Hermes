import type { ActionItemSpec } from '@/components/ui/actions-menu'

export const SESSION_ACTIONS_AREA = 'session.actions'

export interface SessionActionContext {
  sessionId: string
  title: string
  profile?: string
  cwd?: string | null
  surface: 'row' | 'tab'
}

export interface SessionActionContributionData {
  label: string | ((context: SessionActionContext) => string)
  icon?: ActionItemSpec['icon']
  disabled?: boolean | ((context: SessionActionContext) => boolean)
  onSelect: (context: SessionActionContext) => void | Promise<void>
}

export function isSessionActionContributionData(value: unknown): value is SessionActionContributionData {
  if (!value || typeof value !== 'object') {
    return false
  }
  const data = value as Partial<SessionActionContributionData>

  return (typeof data.label === 'string' || typeof data.label === 'function') && typeof data.onSelect === 'function'
}
