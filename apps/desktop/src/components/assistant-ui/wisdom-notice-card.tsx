import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  acknowledgeWisdomNotifications,
  getWisdomInstallations,
  type ProfileScope
} from '@/hermes'
import { useI18n } from '@/i18n'
import { notifyError } from '@/store/notifications'

export function WisdomNoticeCard({ profile }: { profile?: ProfileScope }) {
  const { t } = useI18n()
  const copy = t.skills.collective
  const [events, setEvents] = useState<Array<Record<string, unknown>>>([])

  useEffect(() => {
    let active = true

    const refresh = async () => {
      try {
        const result = await getWisdomInstallations(profile)

        if (active) {
          setEvents(result.notifications)
        }
      } catch {
        // The notice is an enhancement to the transcript. An unavailable or
        // unconfigured Wisdom plane must not make ordinary chat unusable.
        if (active) {
          setEvents([])
        }
      }
    }

    void refresh()
    const timer = window.setInterval(() => void refresh(), 30_000)

    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [profile])

  if (events.length === 0) {return null}

  return (
    <section
      aria-label="Collective Wisdom notifications"
      className="mb-(--conversation-turn-gap) rounded-lg border border-blue-600/40 bg-(--ui-chat-surface-background) p-4"
    >
      <h2 className="text-xs font-medium">{copy.notifications}</h2>
      <ul className="mt-2 space-y-1 text-[0.68rem] text-muted-foreground">
        {events.slice(0, 8).map((event, index) => (
          <li className="break-all" key={String(event.event_id ?? index)}>
            {String(event.kind ?? 'update')} · {String(event.skill_id ?? 'skill')}
            {event.version ? ` · v${String(event.version)}` : ''}
          </li>
        ))}
      </ul>
      <div className="mt-3 flex justify-end">
        <Button
          onClick={async () => {
            try {
              await acknowledgeWisdomNotifications(profile)
              setEvents([])
            } catch (error) {
              notifyError(error, 'Could not acknowledge Wisdom notifications')
            }
          }}
          size="sm"
          variant="outline"
        >
          {copy.markSeen}
        </Button>
      </div>
    </section>
  )
}
