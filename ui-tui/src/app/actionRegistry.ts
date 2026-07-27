import type { ActionContext, ActionGroup, RankedAction, TuiAction } from '../domain/actions.js'
import { fuzzyScoreMulti } from '../lib/fuzzy.js'

const enabled = () => ({ status: 'enabled' as const })

const slashAction = (
  id: string,
  title: string,
  group: ActionGroup,
  command: string,
  options: Partial<Pick<TuiAction, 'aliases' | 'availability' | 'description' | 'shortcut'>> = {}
): TuiAction => ({
  ...options,
  availability: options.availability ?? enabled,
  group,
  id,
  run: ctx => ctx.dispatchSlash(command),
  title
})

const GROUP_ORDER: readonly ActionGroup[] = [
  'suggested',
  'session',
  'context',
  'model-profile',
  'agents',
  'capabilities',
  'view',
  'system'
]

const groupIndex = (group: ActionGroup) => GROUP_ORDER.indexOf(group)

const stableCompare = (a: TuiAction, b: TuiAction) =>
  groupIndex(a.group) - groupIndex(b.group) || a.title.localeCompare(b.title) || a.id.localeCompare(b.id)

const rankAction = (action: TuiAction, query: string): number | null => {
  const normalized = query.trim().toLowerCase()

  if (!normalized) {
    return 0
  }

  const title = action.title.toLowerCase()
  const aliases = action.aliases?.map(alias => alias.toLowerCase()) ?? []

  const prefixScore = title.startsWith(normalized)
    ? 12_000 - title.length
    : aliases.some(alias => alias.startsWith(normalized))
      ? 11_000 - Math.min(...aliases.filter(alias => alias.startsWith(normalized)).map(alias => alias.length))
      : null

  if (prefixScore !== null) {
    return prefixScore
  }

  const fields = [action.title, ...(action.aliases ?? []), action.description ?? '']
  const matches = fields.map(field => fuzzyScoreMulti(field, normalized)).filter(match => match !== null)

  return matches.length ? Math.max(...matches.map(match => match.score)) : null
}

export class ActionRegistry {
  readonly #actions = new Map<string, TuiAction>()

  constructor(actions: readonly TuiAction[] = []) {
    actions.forEach(action => this.register(action))
  }

  register(action: TuiAction): void {
    if (this.#actions.has(action.id)) {
      throw new Error(`Duplicate action id: ${action.id}`)
    }

    this.#actions.set(action.id, action)
  }

  get(id: string): TuiAction | undefined {
    return this.#actions.get(id)
  }

  list(): TuiAction[] {
    return [...this.#actions.values()].sort(stableCompare)
  }

  search(query: string, ctx: ActionContext): RankedAction[] {
    return this.list()
      .map(action => ({ action, availability: action.availability(ctx), score: rankAction(action, query) }))
      .filter(
        (result): result is RankedAction & { score: number } =>
          result.availability.status !== 'hidden' && result.score !== null
      )
      .sort((a, b) => b.score - a.score || stableCompare(a.action, b.action))
  }
}

const unavailableWhileBusy = (ctx: ActionContext) =>
  ctx.busy ? ({ reason: 'Wait for the active turn to finish', status: 'disabled' } as const) : enabled()

export const ACTION_REGISTRY = new ActionRegistry([
  slashAction('session.new', 'New session', 'session', '/new', {
    aliases: ['clear', 'start fresh'],
    availability: unavailableWhileBusy,
    description: 'End this conversation and start a fresh session'
  }),
  slashAction('session.switch', 'Switch session', 'session', '/sessions', {
    aliases: ['sessions', 'resume'],
    description: 'Browse live and resumable sessions',
    shortcut: 'Ctrl+X'
  }),
  slashAction('model.switch', 'Switch model', 'model-profile', '/model', {
    aliases: ['model', 'provider'],
    availability: unavailableWhileBusy,
    description: 'Choose the model for this session'
  }),
  slashAction('agents.open', 'Open agent activity', 'agents', '/agents', {
    aliases: ['agents', 'tasks', 'delegation'],
    description: 'Inspect delegated tasks and agent history'
  }),
  slashAction('skills.open', 'Browse skills', 'capabilities', '/skills', {
    aliases: ['skills'],
    description: 'Browse, inspect, and install skills'
  }),
  slashAction('plugins.open', 'Open plugins', 'capabilities', '/plugins', {
    aliases: ['plugins'],
    description: 'View and manage plugins'
  }),
  slashAction('settings.details', 'Session details & settings', 'view', '/details', {
    aliases: ['settings', 'details'],
    description: 'Control agent detail visibility'
  }),
  slashAction('system.logs', 'View gateway logs', 'system', '/logs', {
    aliases: ['logs', 'diagnostics'],
    description: 'Inspect recent gateway logs'
  }),
  slashAction('system.quit', 'Quit Hermes', 'system', '/quit', {
    aliases: ['quit', 'exit'],
    availability: ctx =>
      ctx.dashboard
        ? { reason: 'Quit is disabled in hosted dashboard chat', status: 'disabled' }
        : { status: 'enabled' },
    description: 'Exit the TUI'
  })
])

let contextProvider: null | (() => ActionContext) = null

export const setActionContextProvider = (provider: () => ActionContext) => {
  contextProvider = provider
}

export const getActionContext = (): ActionContext =>
  contextProvider?.() ?? { busy: false, dashboard: false, dispatchSlash: () => {}, hasSession: false }

export const executeAction = (action: TuiAction): boolean => {
  const ctx = getActionContext()

  if (action.availability(ctx).status !== 'enabled') {
    return false
  }

  void action.run(ctx)

  return true
}
