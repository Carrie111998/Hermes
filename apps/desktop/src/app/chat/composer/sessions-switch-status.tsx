import { useI18n } from '@/i18n'
import type { WorkspaceSendBlockedState } from '@/lib/workspace-send-gate'

export function SessionsSwitchStatus({ state }: { state: null | WorkspaceSendBlockedState }) {
  const { t } = useI18n()

  if (!state) {
    return null
  }

  const messages: Record<WorkspaceSendBlockedState, string> = {
    auth_required: t.composer.workspaceAuthRequiredSendBlocked,
    route_invalid: t.composer.workspaceRouteInvalidSendBlocked,
    switch_failed: t.composer.workspaceSwitchFailedSendBlocked,
    switching: t.composer.sessionsSwitchingSendBlocked,
    unreachable: t.composer.workspaceUnreachableSendBlocked,
    unsupported_build: t.composer.workspaceUnsupportedBuildSendBlocked
  }

  return (
    <p
      aria-live="polite"
      className="text-[0.7rem] text-muted-foreground"
      data-slot="composer-sessions-switch-status"
      role="status"
    >
      {messages[state]}
    </p>
  )
}
