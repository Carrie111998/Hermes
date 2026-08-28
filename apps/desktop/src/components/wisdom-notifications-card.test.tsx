// @vitest-environment jsdom
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { WisdomNotificationsCard } from './wisdom-notifications-card'

const openExternalLink = vi.fn()

vi.mock('@/lib/external-link', () => ({
  openExternalLink: (href: string) => openExternalLink(href)
}))

describe('WisdomNotificationsCard', () => {
  it('renders skill names and versions as an actionable Desktop card', () => {
    const markAllRead = vi.fn().mockResolvedValue(undefined)

    render(
      <WisdomNotificationsCard
        events={[
          {
            category: 'update_available',
            event_id: 'event-1',
            kind: 'updated',
            occurred_at: '2026-08-28T00:00:00Z',
            portal_url: 'https://portal.example/orgs/team/wisdom/skills/skill-1?version=3',
            skill_id: 'skill-1',
            skill_name: 'incident-handoff',
            source_event_ids: ['event-1'],
            version: 3
          }
        ]}
        onMarkAllRead={markAllRead}
      />
    )

    expect(screen.getByText('incident-handoff v3 is available to update.')).toBeTruthy()
    expect(screen.queryByText(/skill-1/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'View skill' }))
    expect(openExternalLink).toHaveBeenCalledWith('https://portal.example/orgs/team/wisdom/skills/skill-1?version=3')
  })
})
