export type ActionGroup =
  | 'suggested'
  | 'session'
  | 'context'
  | 'model-profile'
  | 'agents'
  | 'capabilities'
  | 'view'
  | 'system'

export type Availability =
  | { status: 'enabled' }
  | { reason: string; status: 'disabled' }
  | { status: 'hidden' }

export interface ActionContext {
  busy: boolean
  dashboard: boolean
  dispatchSlash: (command: string) => void
  hasSession: boolean
}

export interface TuiAction {
  aliases?: string[]
  availability: (ctx: ActionContext) => Availability
  description?: string
  group: ActionGroup
  id: string
  run: (ctx: ActionContext) => Promise<void> | void
  shortcut?: string
  title: string
}

export interface RankedAction {
  action: TuiAction
  availability: Exclude<Availability, { status: 'hidden' }>
  score: number
}
