import { useStore } from '@nanostores/react'
import { memo, useState } from 'react'
import type * as React from 'react'

import { ProfileTag } from '@/app/chat/profile-tag'
import { startSessionDrag } from '@/app/chat/session-drag'
import { PlatformAvatar } from '@/app/messaging/platform-icon'
import { openSession } from '@/app/open-session'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'
import type { SessionInfo } from '@/hermes'
import { type Translations, useI18n } from '@/i18n'
import { sessionTitle } from '@/lib/chat-runtime'
import { triggerHaptic } from '@/lib/haptics'
import { middleClickHandlers } from '@/lib/middle-click'
import { handoffOriginSource, sessionSourceLabel } from '@/lib/session-source'
import { coarseElapsed } from '@/lib/time'
import { cn } from '@/lib/utils'
import { $attentionSessionIds } from '@/store/session-states'

import { SessionStatusDot } from '../session-status-dot'

import { SidebarRowBody, SidebarRowGrab, SidebarRowLabel, SidebarRowLead, SidebarRowShell } from './chrome'
import { SessionActionsMenu, SessionContextMenu } from './session-actions-menu'
import { sessionShowsRunningArc } from './session-row-state'
import { useProfilePrewarm } from './use-profile-prewarm'

interface SidebarSessionRowProps extends React.ComponentProps<'div'> {
  session: SessionInfo
  /** TUI-style tree stem for branched sessions (`└─ ` / `├─ `). */
  branchStem?: string
  isPinned: boolean
  isSelected: boolean
  isWorking: boolean
  onArchive: () => void
  onBranch?: () => void
  onDelete: () => void
  onPin: () => void
  onResume: () => void
  reorderable?: boolean
  dragging?: boolean
  dragHandleProps?: React.HTMLAttributes<HTMLElement>
  /** Tag the row with its owning profile (initial chip + tooltip). Used by
   *  flat cross-profile lists — Pinned and search results in the All-profiles
   *  view — where no group header communicates ownership (#66003). */
  showProfile?: boolean
}

const AGE_KEY = { day: 'ageDay', hour: 'ageHour', minute: 'ageMin' } as const

function formatAge(seconds: number, r: Translations['sidebar']['row']): string {
  const { unit, value } = coarseElapsed(Date.now() - seconds * 1000)

  // Under a minute reads as "now" — the sidebar never shows a seconds tick.
  return unit === 'second' ? r.ageNow : `${value}${r[AGE_KEY[unit]]}`
}

function SidebarSessionRowImpl({
  session,
  branchStem,
  isPinned,
  isSelected,
  isWorking,
  onArchive,
  onBranch,
  onDelete,
  onPin,
  onResume,
  reorderable = false,
  dragging = false,
  dragHandleProps,
  showProfile = false,
  className,
  style,
  ref,
  ...rest
}: SidebarSessionRowProps) {
  const { t } = useI18n()
  const r = t.sidebar.row
  const { cancelPrewarm, startPrewarm } = useProfilePrewarm(session.profile)
  const title = sessionTitle(session)
  const age = formatAge(session.last_active || session.started_at, r)
  const handleLabel = `Reorder ${title}`
  // A handed-off session's live source is local, but it originated on a
  // messaging platform — surface that origin as a small badge so e.g. a
  // Telegram thread continued here still reads as Telegram.
  const handoffSource = handoffOriginSource(session.handoff_state, session.handoff_platform)
  const handoffLabel = handoffSource ? (sessionSourceLabel(handoffSource) ?? handoffSource) : null
  // Track the kebab dropdown's open state on the row so the actions slot
  // stays expanded (and the age label stays in flow) while the menu is open.
  // ``group-data-[state=open]`` only resolves when the row's ``.group``
  // ancestor carries ``data-state``; Radix's open signal lives on the inner
  // trigger, so we lift it onto the row here (#75331).
  const [menuOpen, setMenuOpen] = useState(false)
  // True when a clarify prompt in this session is waiting on the user.
  const needsInput = useStore($attentionSessionIds).includes(session.id)

  return (
    <SessionContextMenu
      onArchive={onArchive}
      onBranch={onBranch}
      onDelete={onDelete}
      onPin={onPin}
      pinned={isPinned}
      profile={session.profile}
      sessionId={session.id}
      title={title}
    >
      <SidebarRowShell
        actions={
          // Hover-revealed actions cluster. Anchored to the right edge so the
          // track only takes layout space while the kebab or the relative-age
          // label is actually visible — no standing 22px gutter on idle rows
          // (#75331). ``group-hover``/``group-focus-within`` grow the track on
          // row interaction; ``group-data-[state=open]`` keeps it expanded
          // while the kebab menu stays open.
          //
          // ``data-state`` is lifted onto the row itself (below) so the
          // ``group-data-[state=open]`` selector above fires while the
          // kebab dropdown is open: Radix's open state lives on the inner
          // trigger descendant, not on the row, so we lift the signal up
          // via the SessionActionsMenu's onOpenChange callback.
          <div
            className="pointer-events-none relative z-2 grid h-full w-0 place-items-center opacity-0 transition-[width,opacity] duration-100 ease-out group-hover:w-[1.375rem] group-hover:opacity-100 group-focus-within:w-[1.375rem] group-focus-within:opacity-100 group-data-[state=open]:w-[1.375rem] group-data-[state=open]:opacity-100"
            data-row-actions
          >
            {!isWorking && (
              // The age label sits *inside* the actions track, not absolutely
              // over the title. Putting it in the normal flow reserves real
              // layout space only when the row is hovered (track width > 0),
              // so long titles cannot paint beneath it — they get the
              // ordinary truncation/elision the row already enforces.
              <span className="truncate text-[0.625rem] leading-none text-(--ui-text-tertiary) opacity-0 transition-opacity duration-100 group-hover:opacity-100">
                {age}
              </span>
            )}
            <SessionActionsMenu
              onArchive={onArchive}
              onBranch={onBranch}
              onDelete={onDelete}
              onOpenChange={setMenuOpen}
              onPin={onPin}
              open={menuOpen}
              pinned={isPinned}
              profile={session.profile}
              sessionId={session.id}
              title={title}
            >
              <Button
                aria-label={r.sessionActions}
                className="pointer-events-auto size-5 rounded-[4px] bg-transparent text-transparent transition-colors duration-100 hover:bg-(--ui-control-active-background) hover:text-foreground focus-visible:bg-(--ui-control-active-background) focus-visible:text-foreground focus-visible:ring-0 data-[state=open]:bg-(--ui-control-active-background) data-[state=open]:text-foreground group-hover:text-(--ui-text-tertiary) [&_svg]:size-3.5!"
                size="icon"
                variant="ghost"
              >
                <Codicon name="kebab-vertical" size="0.875rem" />
              </Button>
            </SessionActionsMenu>
          </div>
        }
        className={cn(
          'group row-hover relative',
          isSelected && 'bg-(--ui-row-active-background)',
          isWorking && 'text-foreground',
          // Opaque surface while lifted so the dragged row erases what's under
          // it (translucency let the rows below bleed through).
          dragging && 'z-10 cursor-grabbing bg-(--ui-sidebar-surface-background)',
          className
        )}
        // ``data-state`` on the row lets the actions cluster's
        // ``group-data-[state=open]`` selector fire once the kebab dropdown
        // is open. Radix puts ``data-state`` on the inner trigger, not the
        // row, so we lift the open signal onto the row here via the
        // SessionActionsMenu's onOpenChange callback (#75331).
        data-state={menuOpen ? 'open' : undefined}
        data-working={isWorking ? 'true' : undefined}
        onPointerDown={event => {
          // Reorder drags belong to dnd-kit (the grab handle); the ⋯ actions
          // cluster keeps its own gestures. Everything else on the row —
          // including the row-body BUTTON, the natural grab surface — is a
          // session drag source: a POINTER drag on the shared drag session
          // (never native HTML5 DnD: no macOS snap-back, Esc aborts
          // instantly). Sub-threshold releases stay ordinary clicks, so
          // resume / pin / open-in-window are untouched.
          if ((event.target as HTMLElement).closest('[data-reorder-handle], [data-row-actions]')) {
            return
          }

          startSessionDrag({ id: session.id, profile: session.profile || 'default', title }, event)
        }}
        // Hovering a row from another profile (the all-profiles view) telegraphs
        // a cross-profile resume — start that backend's spawn now so the click
        // doesn't pay the full cold boot. Same-profile rows no-op inside
        // prewarmProfileBackend.
        onPointerEnter={startPrewarm}
        onPointerLeave={cancelPrewarm}
        ref={ref}
        style={style}
        {...rest}
      >
        {sessionShowsRunningArc({ isWorking, needsInput }) && (
          <span aria-hidden="true" className="arc-border arc-row" />
        )}
        <SidebarRowBody
          className={cn('z-0', branchStem && 'pl-3.5')}
          // Middle-click = open in a new tab (browser muscle memory).
          {...middleClickHandlers(() => {
            triggerHaptic('selection')
            openSession(session.id, () => undefined, 'tab')
          })}
          onClick={event => {
            const mod = event.metaKey || event.ctrlKey

            // ⇧⌘-click → pop into its own window (needs standalone windows).
            if (mod && event.shiftKey) {
              event.preventDefault()
              event.stopPropagation()
              triggerHaptic('selection')
              openSession(session.id, () => undefined, 'window')

              return
            }

            // ⌘/⌃-click → open in a new tab (stack into main).
            if (mod) {
              event.preventDefault()
              event.stopPropagation()
              triggerHaptic('selection')
              openSession(session.id, () => undefined, 'tab')

              return
            }

            // ⇧-click → pin.
            if (event.shiftKey) {
              event.preventDefault()
              event.stopPropagation()
              triggerHaptic('selection')
              onPin()

              return
            }

            onResume()
          }}
        >
          {reorderable ? (
            <SidebarRowGrab
              ariaLabel={handleLabel}
              dragging={dragging}
              dragHandleProps={dragHandleProps}
              leadClassName={needsInput ? 'overflow-visible' : undefined}
            >
              <SessionStatusDot
                branchStem={branchStem}
                className="transition-opacity group-hover/handle:opacity-0 group-focus-within/handle:opacity-0"
                session={session}
                storedSessionId={session.id}
              />
            </SidebarRowGrab>
          ) : (
            <SidebarRowLead className={needsInput ? 'overflow-visible' : 'overflow-hidden'}>
              <SessionStatusDot branchStem={branchStem} session={session} storedSessionId={session.id} />
            </SidebarRowLead>
          )}
          {handoffSource && handoffLabel ? (
            <Tip label={r.handoffOrigin(handoffLabel)}>
              <PlatformAvatar
                className="size-4 rounded-[4px] text-[0.5rem] [&_svg]:size-2.5"
                platformId={handoffSource}
                platformName={handoffLabel}
              />
            </Tip>
          ) : null}
          <SidebarRowLabel className="flex-1 font-normal group-hover:text-foreground group-data-[working=true]:text-foreground/90">
            {title}
          </SidebarRowLabel>
          {showProfile && <ProfileTag profile={session.profile} />}
        </SidebarRowBody>
      </SidebarRowShell>
    </SessionContextMenu>
  )
}

// The sidebar re-renders on every stream tick ($sessions/$workingSessionIds
// churn), and it stays mounted beneath every overlay — so an unmemoized row
// re-rendered the whole list (and its Codicon/label/status-dot subtree) on each
// delta, bleeding churn into Settings, Cron, Profiles, Artifacts, etc.
//
// The callback props (onArchive/onResume/…) are fresh closures every render by
// design (they close over the row's session id), so a default memo never bails.
// They're pure id-forwarders, though — identical behavior for a given row — so
// the comparator deliberately ignores them and compares only the DATA that
// changes what the row paints. A row whose session/selection/working/pin state
// is unchanged now bails out, even while a sibling session streams.
function rowPropsEqual(a: SidebarSessionRowProps, b: SidebarSessionRowProps): boolean {
  return (
    a.session === b.session &&
    a.isPinned === b.isPinned &&
    a.isSelected === b.isSelected &&
    a.isWorking === b.isWorking &&
    a.branchStem === b.branchStem &&
    a.reorderable === b.reorderable &&
    a.dragging === b.dragging &&
    a.showProfile === b.showProfile &&
    a.dragHandleProps === b.dragHandleProps &&
    a.className === b.className &&
    a.style === b.style
  )
}

export const SidebarSessionRow = memo(SidebarSessionRowImpl, rowPropsEqual)
