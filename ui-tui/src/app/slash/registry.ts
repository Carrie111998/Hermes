import { coreCommands } from './commands/core.js'
import { debugCommands } from './commands/debug.js'
import { opsCommands } from './commands/ops.js'
import { sessionCommands } from './commands/session.js'
import { setupCommands } from './commands/setup.js'
import { subscriptionCommands } from './commands/subscription.js'
import { topupCommands } from './commands/topup.js'
import type { SlashCommand } from './types.js'

const legacyCommands: SlashCommand[] = [
  ...coreCommands,
  ...topupCommands,
  ...sessionCommands,
  ...subscriptionCommands,
  ...opsCommands,
  ...setupCommands,
  ...debugCommands
]

export const SLASH_COMMANDS: SlashCommand[] = legacyCommands

const ACTION_ID_BY_SLASH_NAME = new Map<string, string>([
  ['new', 'session.new'],
  ['clear', 'session.new'],
  ['sessions', 'session.switch'],
  ['session', 'session.switch'],
  ['switch', 'session.switch'],
  ['resume', 'session.switch'],
  ['model', 'model.switch'],
  ['agents', 'agents.open'],
  ['tasks', 'agents.open'],
  ['skills', 'skills.open'],
  ['plugins', 'plugins.open'],
  ['details', 'settings.details'],
  ['detail', 'settings.details'],
  ['logs', 'system.logs'],
  ['quit', 'system.quit'],
  ['exit', 'system.quit']
])

export const actionIdForSlash = (name: string) => ACTION_ID_BY_SLASH_NAME.get(name.toLowerCase())

const byName = new Map<string, SlashCommand>(
  SLASH_COMMANDS.flatMap(cmd => [cmd.name, ...(cmd.aliases ?? [])].map(name => [name, cmd] as const))
)

export const findSlashCommand = (name: string) => byName.get(name.toLowerCase())
