import {
  closestCenter,
  DndContext,
  type DragEndEvent,
  type DragOverEvent,
  type DragStartEvent,
  KeyboardSensor,
  type Modifier,
  PointerSensor,
  useSensor,
  useSensors
} from '@dnd-kit/core'
import {
  arrayMove,
  horizontalListSortingStrategy,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { CodeEditor } from '@/components/chat/code-editor'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ColorSwatches } from '@/components/ui/color-swatches'
import { ContextMenu, ContextMenuContent, ContextMenuItem, ContextMenuTrigger } from '@/components/ui/context-menu'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { Popover, PopoverAnchor, PopoverContent } from '@/components/ui/popover'
import { ProfileGlyph } from '@/components/ui/profile-glyph'
import { Tip, Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { getProfileSoul, updateProfileSoul } from '@/hermes'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import { PROFILE_SWATCHES, profileColorSoft, resolveProfileColor } from '@/lib/profile-color'
import {
  REORDER_DRAG_TRANSITION_CSS,
  REORDER_RAIL_TRANSITION,
  reorderCommitHaptic,
  reorderStepHaptic
} from '@/lib/reorder'
import { cn } from '@/lib/utils'
import { $hasMultipleConnections } from '@/store/connections'
import { notify, notifyError } from '@/store/notifications'
import {
  $activeGatewayProfile,
  $profileColors,
  $profileCreateRequest,
  $profileOrder,
  $profiles,
  $profilesConnectionId,
  $profileScope,
  ALL_PROFILES,
  normalizeProfileKey,
  profileLabel,
  refreshActiveProfile,
  selectProfile,
  setProfileColor,
  setProfileOrder,
  setShowAllProfiles,
  sortByProfileOrder
} from '@/store/profile'
import {
  $profileRemoteOverrides,
  openRemoteOverrideDialog,
  refreshProfileRemoteOverrides
} from '@/store/profile-remote-override'
import { runExportProfileFlow, runImportProfileFlow } from '@/store/profile-share'
import { $connection } from '@/store/session'
import type { ProfileInfo } from '@/types/hermes'

import { CreateProfileDialog } from '../../profiles/create-profile-dialog'
import { DeleteProfileDialog } from '../../profiles/delete-profile-dialog'
import { RenameProfileDialog } from '../../profiles/rename-profile-dialog'
import { PROFILES_ROUTE, SETTINGS_ROUTE } from '../../routes'

import { ProfileRemoteOverrideDialog } from './profile-remote-override-dialog'
import { useProfilePrewarm } from './use-profile-prewarm'
import { useProfileRailRefreshOnActive } from './use-profile-rail-refresh-on-active'

// Past this many profiles the strip of colored squares stops scaling (tiny
// drag targets, endless horizontal scroll), so the rail collapses to a compact
// menu. Drag-reorder and long-press-recolor live only on the squares path.
const PROFILE_DROPDOWN_THRESHOLD = 13

// Neighbors reflow on RAIL_TRANSITION; the dragged square follows the snappier
// DRAG_TRANSITION while closestCenter resolves variable-width slots. Both
// transitions come from the shared reorder primitive (lib/reorder.ts).
const RAIL_TRANSITION = REORDER_RAIL_TRANSITION
const DRAG_TRANSITION = REORDER_DRAG_TRANSITION_CSS

// The active profile is wider than compact initials, so the rail no longer has a
// uniform cell pitch. Keep drags continuous on the x-axis and let closestCenter
// resolve the variable-width positions, while clamping to the occupied strip.
export function clampRailDragX(x: number, minX: number, maxX: number): number {
  return Math.min(maxX, Math.max(minX, x))
}

const clampToRail: Modifier = ({ containerNodeRect, draggingNodeRect, transform }) => {
  if (!draggingNodeRect || !containerNodeRect) {
    return { ...transform, y: 0 }
  }

  const minX = containerNodeRect.left - draggingNodeRect.left
  const maxX = containerNodeRect.right - draggingNodeRect.right

  return { ...transform, x: clampRailDragX(transform.x, minX, maxX), y: 0 }
}

export function mergeCompactProfileOrder(
  allIds: string[],
  compactIds: string[],
  activeId: string,
  overId: string
): string[] {
  const from = compactIds.indexOf(activeId)
  const to = compactIds.indexOf(overId)

  if (from < 0 || to < 0 || from === to) {
    // Referential equality is the caller's no-op signal: no reorder haptic and
    // no persistence write when the drag did not produce a valid move.
    return allIds
  }

  const reordered = arrayMove(compactIds, from, to)
  const compact = new Set(compactIds)
  let cursor = 0

  return allIds.map(id => (compact.has(id) ? reordered[cursor++] : id))
}

// Arc-Spaces-style profile rail at the sidebar foot: the active identity expands
// on the left, inactive profiles stay compact in the scrolling strip, and Manage
// remains pinned right. The default profile's circular initial marks it as the
// machine owner; ordinary profiles use rounded squares.
export function ProfileRail() {
  const { t } = useI18n()
  const p = t.profiles
  const profiles = useStore($profiles)
  const profilesConnectionId = useStore($profilesConnectionId)
  const connection = useStore($connection)
  const scope = useStore($profileScope)
  const gatewayProfile = useStore($activeGatewayProfile)
  const order = useStore($profileOrder)
  const colors = useStore($profileColors)
  const remoteOverrides = useStore($profileRemoteOverrides)
  const multipleConnections = useStore($hasMultipleConnections)
  const navigate = useNavigate()

  const [createOpen, setCreateOpen] = useState(false)
  const [pendingRename, setPendingRename] = useState<null | ProfileInfo>(null)
  const [pendingDelete, setPendingDelete] = useState<null | ProfileInfo>(null)
  const [pendingSoul, setPendingSoul] = useState<null | string>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  // Too many profiles for the square strip → collapse to the select. Declared
  // ahead of the wheel effect, which re-binds when the strip mounts/unmounts.
  const condensed = profiles.length > PROFILE_DROPDOWN_THRESHOLD

  // A plain mouse wheel only emits deltaY; map it to horizontal scroll so the
  // rail is navigable without a trackpad. Trackpad x-scroll (deltaX) passes
  // through. Native + non-passive so we can preventDefault and not bleed the
  // gesture into the sessions list above.
  useEffect(() => {
    const el = scrollRef.current

    if (!el) {
      return
    }

    const onWheel = (event: WheelEvent) => {
      if (el.scrollWidth <= el.clientWidth || Math.abs(event.deltaY) <= Math.abs(event.deltaX)) {
        return
      }

      el.scrollLeft += event.deltaY
      event.preventDefault()
    }

    el.addEventListener('wheel', onWheel, { passive: false })

    return () => el.removeEventListener('wheel', onWheel)
    // `condensed` swaps the strip out for the dropdown (ref goes null/back).
  }, [condensed])

  const isAll = scope === ALL_PROFILES
  const activeKey = normalizeProfileKey(gatewayProfile)
  const defaultProfile = profiles.find(profile => profile.is_default)
  const defaultLabel = defaultProfile ? profileLabel(defaultProfile) : ''
  const rosterCurrent = profilesConnectionId === (connection?.connectionId ?? null)
  const exactActiveProfile = profiles.find(profile => normalizeProfileKey(profile.name) === activeKey)

  const activeProfile = isAll || !rosterCurrent
    ? null
    : exactActiveProfile ?? (activeKey === 'default' ? defaultProfile : null) ?? null

  const onDefault = activeProfile?.is_default === true
  const activeProfileKey = activeProfile ? normalizeProfileKey(activeProfile.name) : null

  const named = sortByProfileOrder(
    profiles.filter(profile => !profile.is_default),
    order
  )

  const compactNamed = isAll ? named : named.filter(profile => normalizeProfileKey(profile.name) !== activeProfileKey)

  const multiProfile = profiles.length > 1

  // distance constraint: a small drag reorders, a tap still selects the profile.
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates })
  )

  // Tick a haptic each time the drag crosses into a new cell, and a satisfying
  // confirm on a committed reorder.
  const lastOverRef = useRef<string | null>(null)

  const handleDragStart = ({ active }: DragStartEvent) => {
    lastOverRef.current = String(active.id)
  }

  const handleDragOver = ({ over }: DragOverEvent) => {
    const id = over ? String(over.id) : null

    if (id && id !== lastOverRef.current) {
      lastOverRef.current = id
      reorderStepHaptic()
    }
  }

  const handleDragEnd = ({ active, over }: DragEndEvent) => {
    lastOverRef.current = null

    if (!over || active.id === over.id) {
      return
    }

    const ids = named.map(profile => profile.name)
    const compactIds = compactNamed.map(profile => profile.name)
    const next = mergeCompactProfileOrder(ids, compactIds, String(active.id), String(over.id))

    if (next !== ids) {
      setProfileOrder(next)
      reorderCommitHaptic()
    }
  }

  // Re-pull the running profile + list on mount, and again whenever the window
  // regains focus/visibility -- a profile created, deleted, or renamed by
  // another surface (Manage Profiles, another window, the CLI) leaves this
  // rail's cached $profiles stale until something re-fetches it. See
  // use-profile-rail-refresh-on-active.ts for the extracted (and tested)
  // wiring.
  useProfileRailRefreshOnActive()

  // Which profiles carry a per-profile remote override (connection.json
  // profiles.<name>) — refreshed whenever the profile list changes so the
  // rail's "remote" badge tracks create/rename/override edits.
  const profileNames = profiles.map(profile => profile.name)
  const profileNamesKey = profileNames.join('\u0000')

  useEffect(() => {
    void refreshProfileRemoteOverrides(profileNamesKey ? profileNamesKey.split('\u0000') : [])
  }, [profileNamesKey])

  // Open the create dialog when the `profile.create` hotkey fires (the dialog
  // state lives here, so the global keybind bumps a request atom we watch).
  const createRequest = useStore($profileCreateRequest)
  const lastCreateRef = useRef(createRequest)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    if (createRequest === lastCreateRef.current) {
      return
    }

    lastCreateRef.current = createRequest
    setCreateOpen(true)
  }, [createRequest])

  return (
    <div aria-label={p.title} className="flex min-w-0 items-center gap-0.5" data-slot="profile-rail" role="group">
      {multiProfile && isAll && (
        <ProfilePill
          active
          glyph="layers"
          label={p.allProfiles}
          onSelect={() =>
            rosterCurrent && defaultProfile ? selectProfile(defaultProfile.name) : setShowAllProfiles(true)
          }
        />
      )}

      {/* Condensed rails cannot preserve individual slots, so keep the active
          identity pinned beside the profile dropdown at that scale. */}
      {multiProfile && condensed && !isAll && activeProfile && (
        <ActiveProfilePill
          color={resolveProfileColor(activeProfile.name, colors)}
          label={profileLabel(activeProfile)}
          onSelect={() => (activeProfile.is_default ? setShowAllProfiles(true) : selectProfile(activeProfile.name))}
          profileName={activeProfile.name}
        />
      )}

      {/* Single-profile: the active default owner's identity next to create. */}
      {!multiProfile && activeProfile?.is_default && (
        <ActiveProfilePill
          color={resolveProfileColor(activeProfile.name, colors)}
          label={profileLabel(activeProfile)}
          onSelect={() => selectProfile(activeProfile.name)}
          profileName={activeProfile.name}
        />
      )}

      {condensed ? (
        // Condensed path: one compact dropdown instead of N squares. No drag
        // reorder, no long-press recolor, no per-square context menu — Manage
        // covers rename/delete at this scale.
        <div className="flex min-w-0 flex-1 items-center gap-1">
          {rosterCurrent && defaultProfile && (isAll || !onDefault) && (
            <OwnerProfileCompact
              color={resolveProfileColor(defaultProfile.name, colors)}
              label={defaultLabel}
              onSelect={() => selectProfile(defaultProfile.name)}
            />
          )}
          <ProfileDropdown
            activeKey={null}
            colors={colors}
            onCreate={() => setCreateOpen(true)}
            onImport={() => void runImportProfileFlow()}
            onSelect={selectProfile}
            profiles={compactNamed}
          />
        </div>
      ) : (
        <div
          className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
          ref={scrollRef}
        >
          {multiProfile && rosterCurrent && defaultProfile &&
            (onDefault ? (
              <ActiveProfilePill
                color={resolveProfileColor(defaultProfile.name, colors)}
                label={defaultLabel}
                onSelect={() => setShowAllProfiles(true)}
                profileName={defaultProfile.name}
              />
            ) : (
              <OwnerProfileCompact
                color={resolveProfileColor(defaultProfile.name, colors)}
                label={defaultLabel}
                onSelect={() => selectProfile(defaultProfile.name)}
              />
            ))}

          {multiProfile && (
            <DndContext
              collisionDetection={closestCenter}
              modifiers={[clampToRail]}
              onDragEnd={handleDragEnd}
              onDragOver={handleDragOver}
              onDragStart={handleDragStart}
              sensors={sensors}
            >
              <SortableContext items={compactNamed.map(profile => profile.name)} strategy={horizontalListSortingStrategy}>
                {/* relative → the strip is the dragged square's offsetParent, so the
                    clamp modifier bounds drags to the occupied cells (not the +). */}
                <div className="relative flex items-center gap-1">
                  {named.map(profile =>
                    !isAll && normalizeProfileKey(profile.name) === activeProfileKey ? (
                      <ActiveProfilePill
                        color={resolveProfileColor(profile.name, colors)}
                        key={profile.name}
                        label={profileLabel(profile)}
                        onSelect={() => selectProfile(profile.name)}
                        profileName={profile.name}
                      />
                    ) : (
                      <ProfileSquare
                        active={false}
                        color={resolveProfileColor(profile.name, colors)}
                        key={profile.name}
                        label={profileLabel(profile)}
                        onConnectRemote={() => openRemoteOverrideDialog(profile.name)}
                        onDelete={() => setPendingDelete(profile)}
                        onEditSoul={() => setPendingSoul(profile.name)}
                        onRecolor={color => setProfileColor(profile.name, color)}
                        onRename={() => setPendingRename(profile)}
                        onSelect={() => selectProfile(profile.name)}
                        profileName={profile.name}
                        remoteHost={remoteOverrides[normalizeProfileKey(profile.name)]?.host ?? null}
                      />
                    )
                  )}
                </div>
              </SortableContext>
            </DndContext>
          )}

          <AddProfileButton label={p.newProfile} onClick={() => setCreateOpen(true)} />
          <ImportProfileButton label={p.importProfile} />
        </div>
      )}

      {/* Always reachable, even with only the default profile: the manage
          overlay is the only place to edit a profile's SOUL.md, and a
          single-profile user must be able to edit the default's persona
          without first creating a throwaway second profile. */}
      <ProfilePill active={false} glyph="ellipsis" label={p.manageProfiles} onSelect={() => navigate(PROFILES_ROUTE)} />

      {/* Multi-gateway discoverability: before a second source exists, a plug
          pinned beside Manage deep-links to the unified Gateways page. Once
          there are several sources, the same action lives in their selector. */}
      {!multipleConnections && (
        <ProfilePill
          active={false}
          glyph="plug"
          label={p.connectGateway}
          onSelect={() => navigate(`${SETTINGS_ROUTE}?tab=gateway`)}
        />
      )}

      {/* Land in the new profile on a fresh chat (selectProfile triggers the
          new-session reset), not stuck on the session you were just in. */}
      <CreateProfileDialog
        onClose={() => setCreateOpen(false)}
        onCreated={async name => {
          await refreshActiveProfile()
          selectProfile(name)
        }}
        open={createOpen}
        profiles={profiles}
      />

      <RenameProfileDialog
        currentName={pendingRename?.name ?? ''}
        isDefault={pendingRename?.is_default ?? false}
        onClose={() => setPendingRename(null)}
        onRenamed={refreshActiveProfile}
        open={pendingRename !== null}
      />

      <DeleteProfileDialog
        onClose={() => setPendingDelete(null)}
        onDeleted={refreshActiveProfile}
        open={pendingDelete !== null}
        profile={pendingDelete}
      />

      <EditSoulDialog onClose={() => setPendingSoul(null)} profileName={pendingSoul} />

      <ProfileRemoteOverrideDialog profileNames={profileNames} />
    </div>
  )
}

// Right-click → Edit SOUL.md for a sidebar profile — the same in-app markdown
// editor as the memory-graph node edit, so a profile's persona is editable
// without opening the Manage overlay.
function EditSoulDialog({ onClose, profileName }: { onClose: () => void; profileName: null | string }) {
  const { t } = useI18n()
  const p = t.profiles
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!profileName) {
      return
    }

    let cancelled = false
    setLoading(true)
    setContent('')

    getProfileSoul(profileName)
      .then(soul => !cancelled && setContent(soul.content))
      .catch(err => !cancelled && notifyError(err, p.failedLoadSoul))
      .finally(() => !cancelled && setLoading(false))

    return () => void (cancelled = true)
  }, [p, profileName])

  const save = async () => {
    if (!profileName) {
      return
    }

    setSaving(true)

    try {
      await updateProfileSoul(profileName, content)
      notify({ kind: 'success', title: p.soulSaved, message: profileName })
      onClose()
    } catch (err) {
      notifyError(err, p.failedSaveSoul)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog onOpenChange={open => !open && !saving && onClose()} open={profileName !== null}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{profileName} · SOUL.md</DialogTitle>
        </DialogHeader>
        <div className="h-80">
          {!loading && profileName && (
            <CodeEditor
              filePath="SOUL.md"
              framed
              initialValue={content}
              key={profileName}
              onCancel={() => !saving && onClose()}
              onChange={setContent}
              onSave={() => void save()}
            />
          )}
        </div>
        <DialogFooter>
          <Button disabled={saving} onClick={onClose} type="button" variant="ghost">
            {t.common.cancel}
          </Button>
          <Button disabled={saving || loading} onClick={() => void save()}>
            {saving ? p.saving : p.saveSoul}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// The "+" create button, shared by both rail render paths.
function AddProfileButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <Tip label={label}>
      <button
        aria-label={label}
        className="grid size-5 shrink-0 place-items-center rounded-[3px] text-(--ui-text-tertiary) opacity-55 transition hover:bg-(--ui-control-hover-background) hover:text-foreground hover:opacity-100"
        onClick={onClick}
        type="button"
      >
        <Codicon name="add" size="0.75rem" />
      </button>
    </Tip>
  )
}

// Import-archive door beside the "+": adopt a shared profile bundle (theme,
// skills, layout) as a new profile. Same chrome as AddProfileButton; the whole
// flow (picker → import → apply overlay → switch) lives in the store.
function ImportProfileButton({ label }: { label: string }) {
  return (
    <Tip label={label}>
      <button
        aria-label={label}
        className="grid size-5 shrink-0 place-items-center rounded-[3px] text-(--ui-text-tertiary) opacity-55 transition hover:bg-(--ui-control-hover-background) hover:text-foreground hover:opacity-100"
        onClick={() => void runImportProfileFlow()}
        type="button"
      >
        <Codicon name="cloud-download" size="0.75rem" />
      </button>
    </Tip>
  )
}

// The condensed rail: every named profile in one compact menu. The trigger
// shows the active profile (tinted initial + name); on default/all scope it
// falls back to the placeholder since the left toggle pill carries that state.
function ProfileDropdown({
  activeKey,
  colors,
  onCreate,
  onImport,
  onSelect,
  profiles
}: {
  activeKey: null | string
  colors: Record<string, string>
  onCreate: () => void
  onImport: () => void
  onSelect: (name: string) => void
  profiles: ProfileInfo[]
}) {
  const { t } = useI18n()
  const p = t.profiles

  const value = activeKey ? (profiles.find(profile => normalizeProfileKey(profile.name) === activeKey)?.name ?? '') : ''
  const activeProfile = profiles.find(profile => profile.name === value)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={p.title}
          className="min-w-0 flex-1 justify-between overflow-hidden px-1 text-(--ui-text-secondary) data-[state=open]:bg-(--ui-control-active-background) data-[state=open]:text-foreground"
          data-slot="profile-dropdown"
          size="xs"
          type="button"
          variant="ghost"
        >
          <span className="flex min-w-0 flex-1 items-center gap-1 overflow-hidden">
            {activeProfile ? (
              <>
                <ProfileGlyph
                  aria-hidden="true"
                  color={resolveProfileColor(activeProfile.name, colors)}
                  isDefault={false}
                  name={activeProfile.name}
                />
                <span className="truncate">{profileLabel(activeProfile)}</span>
              </>
            ) : (
              <span className="truncate">{p.title}</span>
            )}
          </span>
          <Codicon aria-hidden="true" className="shrink-0 opacity-60" name="chevron-down" size="0.875rem" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-48 max-w-72" collisionPadding={8} side="top">
        <DropdownMenuItem onSelect={onCreate}>
          <Codicon aria-hidden="true" name="add" size="0.875rem" />
          <span className="truncate">{p.newProfile}</span>
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onImport}>
          <Codicon aria-hidden="true" name="cloud-download" size="0.875rem" />
          <span className="truncate">{p.importProfile}</span>
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup onValueChange={name => name && onSelect(name)} value={value}>
          {profiles.map(profile => (
            <ProfileDropdownItem
              color={resolveProfileColor(profile.name, colors)}
              key={profile.name}
              label={profileLabel(profile)}
              name={profile.name}
            />
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// One dropdown row per profile — its own component so each row can own a
// hover-intent prewarm timer (see useProfilePrewarm).
function ProfileDropdownItem({ color, label, name }: { color: null | string; label: string; name: string }) {
  const { cancelPrewarm, startPrewarm } = useProfilePrewarm(name)

  return (
    <DropdownMenuRadioItem
      className="min-w-0"
      onPointerEnter={startPrewarm}
      onPointerLeave={cancelPrewarm}
      value={name}
    >
      <span className="flex min-w-0 items-center gap-1.5">
        <ProfileGlyph aria-hidden="true" color={color} isDefault={false} name={name} />
        <span className="truncate">{label}</span>
      </span>
    </DropdownMenuRadioItem>
  )
}

interface ProfilePillProps {
  active: boolean
  // home / All / Manage are glyph action buttons (navigation, not identity).
  glyph: string
  label: string
  onSelect: () => void
}

function ActiveProfilePill({
  color,
  label,
  onSelect,
  profileName
}: {
  color: null | string
  label: string
  onSelect: () => void
  profileName: string
}) {
  const { gateway, requestGateway } = useGatewayRequest()

  const [avatar, setAvatar] = useState<{
    data: string
    gateway: typeof gateway
    profileName: string
  } | null>(null)
  // A gateway/profile switch renders before its passive effects run. Scope the
  // committed image to the identity that fetched it so the previous machine's
  // owner can never flash in the new machine's rail during that first paint.

  const avatarSrc = avatar?.gateway === gateway && avatar.profileName === profileName ? avatar.data : null

  useEffect(() => {
    let live = true

    if (!gateway) {
      return () => {
        live = false
      }
    }

    void requestGateway<{ data?: string; found?: boolean }>('profiles.get_asset', {
      asset: 'avatar',
      name: profileName
    })
      .then(asset => {
        if (live && asset.found && asset.data) {
          setAvatar({ data: asset.data, gateway, profileName })
        }
      })
      .catch(() => undefined)

    return () => {
      live = false
    }
  }, [gateway, profileName, requestGateway])

  return (
    <Tip label={label}>
      <Button
        aria-label={label}
        aria-pressed="true"
        className="max-w-28 bg-(--ui-control-active-background) px-1 text-foreground hover:bg-(--ui-control-hover-background)"
        onClick={onSelect}
        size="xs"
        type="button"
        variant="ghost"
      >
        {avatarSrc ? (
          <img
            alt=""
            aria-hidden="true"
            className="size-4 shrink-0 rounded-full object-cover"
            onError={() => setAvatar(null)}
            src={avatarSrc}
          />
        ) : (
          <ProfileGlyph aria-hidden="true" color={color} isDefault={false} name={label} />
        )}
        <span className="truncate">{label}</span>
      </Button>
    </Tip>
  )
}

function OwnerProfileCompact({
  color,
  label,
  onSelect
}: {
  color: null | string
  label: string
  onSelect: () => void
}) {
  const hue = color ?? 'var(--ui-text-quaternary)'
  const initial = label.replace(/[^a-z0-9]/gi, '').charAt(0) || '?'

  return (
    <Tip label={label}>
      <button
        aria-label={label}
        aria-pressed="false"
        className="grid size-5 shrink-0 place-items-center rounded-full text-[0.5625rem] font-semibold uppercase leading-none opacity-70 ring-1 ring-current ring-offset-1 ring-offset-background transition-opacity hover:opacity-100"
        onClick={onSelect}
        style={{ backgroundColor: profileColorSoft(hue, 24), color: hue }}
        type="button"
      >
        <span data-slot="profile-owner-compact">{initial}</span>
      </button>
    </Tip>
  )
}

function ProfilePill({ active, glyph, label, onSelect }: ProfilePillProps) {
  return (
    <Tip label={label}>
      <Button
        aria-label={label}
        aria-pressed={active}
        className={cn(
          'bg-transparent text-(--ui-text-tertiary) hover:bg-(--ui-control-hover-background) hover:text-foreground',
          active && 'bg-(--ui-control-active-background) text-foreground'
        )}
        onClick={onSelect}
        size="icon-xs"
        type="button"
        variant="ghost"
      >
        <Codicon name={glyph} size="0.875rem" />
      </Button>
    </Tip>
  )
}

interface ProfileSquareProps {
  active: boolean
  color: null | string
  label: string
  profileName: string
  onSelect: () => void
  onRecolor: (color: null | string) => void
  onRename: () => void
  onEditSoul: () => void
  onConnectRemote: () => void
  onDelete: () => void
  // hostname[:port] of this profile's remote override, or null when the
  // profile runs locally. Drives the "remote" badge on the square.
  remoteHost: null | string
}

// Hold this long without moving (a drag would have started first) to open the
// color picker — the "hard press" gesture, distinct from tap-to-select.
const LONG_PRESS_MS = 450

// A profile *is* its colored square — no icon-button chrome. Soft profile-tint
// fill + the initial in the full color; the active one pops to full opacity with
// a color ring. These pack tightly so the rail reads as a strip of profiles,
// drag-sort to reorder (a tap below the drag threshold still selects), and
// right-click to rename/delete. The button carries both the tooltip and
// context-menu triggers via nested asChild Slots, so a single element keeps the
// dnd listeners, hover tip, and right-click menu.
function ProfileSquare({
  active,
  color,
  label,
  onConnectRemote,
  onDelete,
  onEditSoul,
  onRecolor,
  onRename,
  onSelect,
  profileName,
  remoteHost
}: ProfileSquareProps) {
  const { t } = useI18n()
  const p = t.profiles
  const hue = color ?? 'var(--ui-text-quaternary)'
  const [pickerOpen, setPickerOpen] = useState(false)
  const pressTimer = useRef<null | number>(null)
  const suppressClick = useRef(false)
  // Hovering a square telegraphs the switch — start that profile's backend
  // spawn now so a cold click doesn't pay the full boot.
  const { cancelPrewarm, startPrewarm } = useProfilePrewarm(profileName)

  const { attributes, isDragging, listeners, setNodeRef, transform, transition } = useSortable({
    id: profileName,
    transition: RAIL_TRANSITION
  })

  const clearPress = () => {
    if (pressTimer.current != null) {
      clearTimeout(pressTimer.current)
      pressTimer.current = null
    }
  }

  // A real drag (movement past the dnd threshold) cancels the pending hold, so a
  // reorder never doubles as a color pick. Also tidy up on unmount.
  useEffect(() => {
    if (isDragging) {
      clearPress()
    }
  }, [isDragging])
  useEffect(() => clearPress, [])

  const base = CSS.Transform.toString(transform)
  const ring = active ? `inset 0 0 0 1.5px ${hue}` : ''
  const lift = isDragging ? '0 6px 16px -4px rgb(0 0 0 / 0.4)' : ''

  const pickColor = (next: null | string) => {
    onRecolor(next)
    setPickerOpen(false)
    triggerHaptic('selection')
  }

  return (
    <Popover onOpenChange={setPickerOpen} open={pickerOpen}>
      <ContextMenu>
        <TooltipProvider delayDuration={0}>
          <Tooltip>
            <PopoverAnchor asChild>
              <ContextMenuTrigger asChild>
                <TooltipTrigger asChild>
                  <button
                    className={cn(
                      'relative grid size-5 shrink-0 cursor-grab touch-none select-none place-items-center rounded-[3px] text-[0.5625rem] font-semibold uppercase leading-none transition-opacity hover:opacity-100',
                      active ? 'opacity-100' : 'opacity-55',
                      isDragging && 'z-10 cursor-grabbing opacity-100'
                    )}
                    ref={setNodeRef}
                    style={{
                      backgroundColor: profileColorSoft(hue, active ? 30 : 22),
                      boxShadow: [ring, lift].filter(Boolean).join(', ') || undefined,
                      color: color ?? undefined,
                      // Glide the dragged square between snapped cells with a little
                      // overshoot (no scale — the overflow-x strip would clip it).
                      transform: base,
                      transition: isDragging ? DRAG_TRANSITION : transition
                    }}
                    type="button"
                    {...attributes}
                    {...listeners}
                    aria-label={label}
                    aria-pressed={active}
                    // Hold-to-recolor rides alongside the dnd pointer listener (call
                    // it first so drag tracking still arms), then a timer opens the
                    // picker and flags the trailing click so it doesn't also select.
                    onClick={() => {
                      if (suppressClick.current) {
                        suppressClick.current = false

                        return
                      }

                      onSelect()
                    }}
                    onPointerCancel={clearPress}
                    onPointerDown={event => {
                      listeners?.onPointerDown?.(event)

                      if (event.button !== 0) {
                        return
                      }

                      suppressClick.current = false
                      clearPress()
                      pressTimer.current = window.setTimeout(() => {
                        suppressClick.current = true
                        triggerHaptic('success')
                        setPickerOpen(true)
                      }, LONG_PRESS_MS)
                    }}
                    onPointerEnter={startPrewarm}
                    onPointerLeave={() => {
                      clearPress()
                      cancelPrewarm()
                    }}
                    onPointerUp={clearPress}
                  >
                    {label.replace(/[^a-z0-9]/gi, '').charAt(0) || '?'}
                    {/* The "remote" badge: a tiny globe pinned to the corner of an
                        overridden profile's square, so which profiles leave this
                        machine is visible at a glance (#91349). */}
                    {remoteHost && (
                      <span
                        aria-hidden="true"
                        className="absolute -right-0.5 -top-0.5 grid size-2 place-items-center rounded-full bg-(--ui-panel-background)"
                        data-slot="profile-remote-badge"
                      >
                        <Codicon name="globe" size="0.5rem" />
                      </span>
                    )}
                  </button>
                </TooltipTrigger>
              </ContextMenuTrigger>
            </PopoverAnchor>
            <TooltipContent>{label}</TooltipContent>
          </Tooltip>
        </TooltipProvider>

        {/* The rail sits at the very bottom, so pad off the chrome (esp. the
            statusbar) — Radix then flips the menu up instead of squishing it. */}
        <ContextMenuContent
          aria-label={p.actions}
          className="w-40"
          collisionPadding={{ bottom: 44, left: 8, right: 8, top: 8 }}
          // Menu close refocuses the trigger — which doubles as the popover
          // anchor — so the picker reads it as focus-outside and dies on open.
          // Suppress the refocus and the picker survives.
          onCloseAutoFocus={event => event.preventDefault()}
        >
          <ContextMenuItem onSelect={() => setPickerOpen(true)}>
            <Codicon name="symbol-color" size="0.875rem" />
            <span>{p.color}</span>
          </ContextMenuItem>
          <ContextMenuItem onSelect={onRename}>
            <Codicon name="text-size" size="0.875rem" />
            <span>{p.renameMenu}</span>
          </ContextMenuItem>
          <ContextMenuItem onSelect={onEditSoul}>
            <Codicon name="edit" size="0.875rem" />
            <span>{p.editSoul}</span>
          </ContextMenuItem>
          <ContextMenuItem onSelect={() => void runExportProfileFlow(label)}>
            <Codicon name="package" size="0.875rem" />
            <span>{p.exportProfile}</span>
          </ContextMenuItem>
          <ContextMenuItem onSelect={onConnectRemote}>
            <Codicon name="globe" size="0.875rem" />
            <span>{remoteHost ? p.remoteOverride.badge(remoteHost) : p.remoteOverride.menuItem}</span>
          </ContextMenuItem>
          <ContextMenuItem
            className="text-destructive focus:text-destructive"
            onSelect={onDelete}
            variant="destructive"
          >
            <Codicon name="trash" size="0.875rem" />
            <span>{t.common.delete}</span>
          </ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>

      <PopoverContent
        aria-label={p.colorFor}
        className="w-auto p-2"
        collisionPadding={{ bottom: 44, left: 8, right: 8, top: 8 }}
        side="top"
      >
        <ColorSwatches
          clearIcon="sync"
          clearLabel={p.autoColor}
          onChange={pickColor}
          swatches={PROFILE_SWATCHES}
          swatchLabel={p.setColor}
          value={color}
        />
      </PopoverContent>
    </Popover>
  )
}
