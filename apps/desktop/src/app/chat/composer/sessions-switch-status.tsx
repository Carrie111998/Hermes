import { useI18n } from '@/i18n'

export function SessionsSwitchStatus({ blocked }: { blocked: boolean }) {
  const { t } = useI18n()

  if (!blocked) {
    return null
  }

  return (
    <p
      aria-live="polite"
      className="text-[0.7rem] text-muted-foreground"
      data-slot="composer-sessions-switch-status"
      role="status"
    >
      {t.composer.sessionsSwitchingSendBlocked}
    </p>
  )
}
