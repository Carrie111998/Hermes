import { translateNow } from '@/i18n'
import type { ActionStatusResponse, UpdateActionResponse } from '@/types/hermes'

type UpdateRefusalResponse = Extract<UpdateActionResponse, { ok: false }>

/** Convert a synchronous update refusal into the same terminal state rendered
 * for polled actions, while retaining only remediation the server supplied. */
export function terminalUpdateRefusal(response: UpdateRefusalResponse): {
  guidance: string
  status: ActionStatusResponse
} {
  const message =
    (typeof response.message === 'string' && response.message.trim()) ||
    translateNow('updates.applyStatus.notAvailable')

  const command = typeof response.update_command === 'string' ? response.update_command.trim() || null : null
  const guidance = command && !message.includes(command) ? `${message}\n${command}` : message

  return {
    guidance,
    status: {
      action_id: typeof response.action_id === 'string' ? response.action_id : undefined,
      exit_code: response.error === 'image_managed_update_refused' ? 2 : 1,
      lines: command && !message.includes(command) ? [message, command] : [message],
      name: typeof response.name === 'string' && response.name ? response.name : 'hermes-update',
      pid: null,
      running: false
    }
  }
}
