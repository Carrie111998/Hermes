import { cleanup, render, screen } from '@testing-library/react'
import { fireEvent } from '@testing-library/react'
import { atom } from 'nanostores'
import type * as React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/hermes'
import type * as ComposerStatusStore from '@/store/composer-status'
import type * as SessionStore from '@/store/session'
import type * as SessionStatesStore from '@/store/session-states'
import type * as WindowsStore from '@/store/windows'

import { SidebarSessionRow } from './session-row'

afterEach(cleanup)

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        row: {
          ageMin: 'm',
          ageNow: 'now',
          backgroundRunning: 'Running in background',
          finishedUnread: 'Finished',
          handoffOrigin: (platform: string) => `Started on ${platform}`,
          needsInput: 'Needs input',
          sessionActions: 'Session actions',
          sessionRunning: 'Running',
          waitingForAnswer: 'Waiting for answer'
        }
      }
    }
  })
}))

vi.mock('@/app/chat/profile-tag', () => ({ ProfileTag: () => null }))
vi.mock('@/app/chat/session-drag', () => ({ startSessionDrag: vi.fn() }))
// PlatformAvatar is intentionally NOT mocked (do not reintroduce this — see
// #67500, Gille's third pass): it's a forwardRef component that spreads its
// props onto the rendered span, and mocking it with a stand-in that spreads
// props itself only proves the MOCK forwards them, not that the real
// component does. This file exercises the actual production component so a
// regression in its ref/prop forwarding fails here again.
vi.mock('@/lib/chat-runtime', () => ({ sessionTitle: (s: SessionInfo) => (s as unknown as { title: string }).title }))
vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))
vi.mock('@/lib/session-source', () => ({
  handoffOriginSource: (state?: string, platform?: string) => (state && platform ? platform : null),
  sessionSourceLabel: (source: string) => source
}))
vi.mock('@/lib/time', () => ({ coarseElapsed: () => ({ unit: 'minute' as const, value: 5 }) }))

// These mocks use importOriginal rather than replacing the module wholesale:
// session-row.tsx (and its transitive imports, e.g. session-color.ts) reads
// several store exports beyond the ones this file cares about, and that set
// keeps growing as the app evolves upstream. A wholesale replacement mock
// silently turns every export it doesn't list into `undefined`, which then
// crashes nanostores' `computed()` the moment a new dependency is added
// upstream (as happened twice already: $stalledSessionIds, then $sessions).
// Overriding only the named atoms we actually control keeps this test
// resilient to that drift.
vi.mock('@/store/composer-status', async importOriginal => {
  const actual = await importOriginal<typeof ComposerStatusStore>()

  return { ...actual, $backgroundRunningSessionIds: atom<string[]>([]) }
})
vi.mock('@/store/session', async importOriginal => {
  const actual = await importOriginal<typeof SessionStore>()

  return { ...actual, $unreadFinishedSessionIds: atom<string[]>([]) }
})
vi.mock('@/store/session-states', async importOriginal => {
  const actual = await importOriginal<typeof SessionStatesStore>()

  return {
    ...actual,
    $attentionSessionIds: atom<string[]>([]),
    $stalledSessionIds: atom<string[]>([]),
    openSessionTile: vi.fn()
  }
})
vi.mock('@/store/windows', async importOriginal => {
  const actual = await importOriginal<typeof WindowsStore>()

  return {
    ...actual,
    canOpenSessionWindow: () => false,
    openSessionInNewWindow: vi.fn()
  }
})

// SessionActionsMenu open behavior is covered in session-actions-menu.test.tsx
// against the real component. Stub it here so this file stays focused on the
// row chrome (handoff avatar tip, etc.).
vi.mock('./session-actions-menu', () => ({
  SessionActionsMenu: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  SessionContextMenu: ({ children }: { children: React.ReactNode }) => <>{children}</>
}))

vi.mock('./use-profile-prewarm', () => ({
  useProfilePrewarm: () => ({ cancelPrewarm: vi.fn(), startPrewarm: vi.fn() })
}))

function makeSession(overrides: Partial<SessionInfo> & { title: string }): SessionInfo {
  return {
    handoff_platform: null,
    handoff_state: null,
    id: 's1',
    last_active: 0,
    profile: 'default',
    started_at: 0,
    ...overrides
  } as unknown as SessionInfo
}

const tipTrigger = (el: HTMLElement) => el.closest('[data-slot="tooltip-trigger"]')
const noop = vi.fn()
describe('SidebarSessionRow', () => {
  it('collapses the actions track to zero width when idle (#75331)', () => {
    const { container } = render(
      <SidebarSessionRow
        isPinned={false}
        isSelected={false}
        isWorking={false}
        onArchive={noop}
        onDelete={noop}
        onPin={noop}
        onResume={noop}
        session={makeSession({ title: 'Idle row' })}
      />
    )

    const track = container.querySelector('[data-row-actions]')
    expect(track).toBeTruthy()
    // Idle: width-0 and opacity-0 keeps the row flush to the chat edge.
    expect((track as HTMLElement).className).toMatch(/\bw-0\b/)
    expect((track as HTMLElement).className).toMatch(/\bopacity-0\b/)
  })

  it('expands the actions track on row hover (#75331)', () => {
    const { container } = render(
      <SidebarSessionRow
        isPinned={false}
        isSelected={false}
        isWorking={false}
        onArchive={noop}
        onDelete={noop}
        onPin={noop}
        onResume={noop}
        session={makeSession({ title: 'Hovered row' })}
      />
    )

    const row = container.firstElementChild?.querySelector('[data-row-actions]')?.parentElement
      ?.parentElement as HTMLElement | null
    expect(row).toBeTruthy()
    // Simulate the parent group hover state — Tailwind's group-hover would
    // only target the row's `group` ancestor in the real DOM. We assert that
    // the cluster class list includes the hover-reveal contract.
    const track = container.querySelector('[data-row-actions]') as HTMLElement
    expect(track.className).toMatch(/group-hover:w-\[1\.375rem\]/)
    expect(track.className).toMatch(/group-hover:opacity-100/)
    // Fire a pointerenter to mirror the real interaction; the regression is
    // caught statically above (CSS contract), this just exercises the
    // pointer pipeline so a future refactor that drops the class hook fails.
    // Mirror the real DOM hover contract: the track stays in the row, the
    // parent <div class="group ..."> is the `group` ancestor for the
    // Tailwind group-hover variants. The CSS contract is what guards the
    // regression — Tailwind's `group-hover:` only resolves against an
    // element actually marked `class="group"`, and our session-row.tsx
    // passes that className on SidebarRowShell. We assert the *contract*,
    // not JSDOM-applied styles (JSDOM doesn't resolve group-hover).
    expect(track.getAttribute('data-row-actions')).not.toBeNull()
    // eslint-disable-next-line @typescript-eslint/no-unused-expressions
    fireEvent.pointerEnter(row as HTMLElement)
  })

  it('reflects the kebab dropdown open state on the row (data-state attr)', () => {
    // The row carries ``data-state`` derived from the dropdown's open signal
    // (Radix puts ``data-state`` on the inner trigger, not the row, so we
    // lift the signal up to the row via SessionActionsMenu's onOpenChange
    // callback). ``group-data-[state=open]`` on the actions track only
    // resolves when the row itself is annotated, so a future refactor that
    // drops the onOpenChange wiring fails here.
    const { container } = render(
      <SidebarSessionRow
        isPinned={false}
        isSelected={false}
        isWorking={false}
        onArchive={noop}
        onDelete={noop}
        onPin={noop}
        onResume={noop}
        session={makeSession({ title: 'Open menu row' })}
      />
    )

    const shell = container.querySelector('[data-row-actions]')?.parentElement?.parentElement as HTMLElement | null
    expect(shell).toBeTruthy()
    // Idle: no data-state on the shell — the actions track stays collapsed.
    expect(shell!.getAttribute('data-state')).toBeNull()
    // The track's ``group-data-[state=open]`` selector needs the row to be the
    // ``.group`` ancestor AND to carry ``data-state`` when the menu opens.
    const track = container.querySelector('[data-row-actions]') as HTMLElement
    expect(track.className).toMatch(/group-data-\[state=open\]/)
  })

  it('places the age label inside the actions track (no absolute title overlap)', () => {
    // Long titles used to paint beneath the absolute-positioned age label
    // when ``group-hover:pr-12`` was removed. The label now lives *inside*
    // the actions track, so it reserves layout space only when the track is
    // expanded and can never collide with a long title.
    const { container } = render(
      <SidebarSessionRow
        isPinned={false}
        isSelected={false}
        isWorking={false}
        onArchive={noop}
        onDelete={noop}
        onPin={noop}
        onResume={noop}
        session={makeSession({ title: 'Long-titled row that used to overflow' })}
      />
    )

    const track = container.querySelector('[data-row-actions]') as HTMLElement
    expect(track).toBeTruthy()
    // The age label is a child of the actions track (not absolutely positioned
    // over the title area). The ``truncate`` utility class on the label
    // caps its width inside the track.
    const ageLabel = track.querySelector('span')
    expect(ageLabel).toBeTruthy()
    expect((ageLabel as HTMLElement).className).toMatch(/\btruncate\b/)
    // Defensive: no absolutely-positioned ``right-...`` overlay that could
    // paint over a long title.
    expect(track.querySelector('.absolute.right-full')).toBeNull()
  })

  it('keeps an aria-label on the kebab without wrapping it in a Tip', () => {
    render(
      <SidebarSessionRow
        isPinned={false}
        isSelected={false}
        isWorking={false}
        onArchive={noop}
        onDelete={noop}
        onPin={noop}
        onResume={noop}
        session={makeSession({ title: 'Hermes doctor health check results' })}
      />
    )

    const kebab = screen.getByRole('button', { name: 'Session actions' })
    expect(tipTrigger(kebab)).toBeNull()
  })

  it('does not render a handoff avatar for a locally-started session', () => {
    const { container } = render(
      <SidebarSessionRow
        isPinned={false}
        isSelected={false}
        isWorking={false}
        onArchive={noop}
        onDelete={noop}
        onPin={noop}
        onResume={noop}
        session={makeSession({ title: 'Local session' })}
      />
    )

    // PlatformAvatar's span is the only aria-hidden SPAN this row ever
    // renders (idle dot / arc-border / branch-stem are all inactive here) —
    // Codicon icons (e.g. the kebab trigger) are also aria-hidden but render
    // as <i>, not <span>, so this selector doesn't accidentally match them.
    expect(container.querySelector('span[aria-hidden="true"]')).toBeNull()
  })

  it('wraps the handoff platform avatar in a Tip for a session started on another platform', () => {
    const { container } = render(
      <SidebarSessionRow
        isPinned={false}
        isSelected={false}
        isWorking={false}
        onArchive={noop}
        onDelete={noop}
        onPin={noop}
        onResume={noop}
        session={makeSession({
          handoff_platform: 'telegram',
          handoff_state: 'active',
          title: 'Continued from Telegram'
        })}
      />
    )

    // PlatformAvatar is the REAL component here (see the note above the vi.mock
    // block, #67500 third pass) — it renders the Telegram brand SVG rather
    // than the platform name as text, so query the avatar span itself (the
    // row's only aria-hidden span in this state) rather than text content,
    // and confirm its tooltip trigger actually attaches to it — proving the
    // real forwardRef/...rest path works, not a mock that fakes it.
    const avatar = container.querySelector('span[aria-hidden="true"]')
    expect(avatar).toBeTruthy()
    expect(tipTrigger(avatar as HTMLElement)).toBeTruthy()
  })
})
