const MANAGED_BILLING_SLASH_COMMANDS = new Set(['/subscription', '/topup', '/upgrade'])

const MANAGED_HIDDEN_ADVANCED_FIELDS = new Set([
  'toolsets',
  'terminal.backend',
  'terminal.docker_image',
  'terminal.singularity_image',
  'terminal.modal_image',
  'terminal.daytona_image',
  'updates.non_interactive_local_changes'
])

export function isManagedSettingsViewVisible(view: string, managed: boolean): boolean {
  return !managed || view !== 'billing'
}

export function isManagedConfigFieldVisible(key: string, managed: boolean): boolean {
  return !managed || !MANAGED_HIDDEN_ADVANCED_FIELDS.has(key)
}

export function isManagedBillingSlashCommand(command: string, managed: boolean): boolean {
  if (!managed) {
    return false
  }

  const normalized = command.trim().split(/\s+/, 1)[0]?.toLowerCase() ?? ''
  const withSlash = normalized.startsWith('/') ? normalized : `/${normalized}`

  return MANAGED_BILLING_SLASH_COMMANDS.has(withSlash)
}
