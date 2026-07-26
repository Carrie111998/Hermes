import { useStore } from '@nanostores/react'
import { Dialog as DialogPrimitive } from 'radix-ui'
import { useEffect, useState } from 'react'

import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { useI18n } from '@/i18n'
import { AlertTriangle, FolderOpen, Trash2 } from '@/lib/icons'
import { projectPathLabel } from '@/lib/project-paths'
import { cn } from '@/lib/utils'
import { probeRecentProjects } from '@/store/project-switcher'
import { $recentProjectRows, forgetRecentProject } from '@/store/recent-projects'

interface ProjectSwitcherDialogProps {
  /** Workspace currently anchored, so it can be flagged in the list. */
  activeCwd?: null | string
  onOpenChange: (open: boolean) => void
  /** Pick an existing recent workspace. */
  onSelect: (path: string) => void
  /** Browse for a folder that isn't in the list yet. */
  onOpenFolder: () => void
  open: boolean
}

/**
 * Recent-projects switcher: a type-to-filter list of the workspaces you've
 * opened, so jumping between projects doesn't mean re-picking a folder.
 *
 * Deliberately a PICKER, not a second chat surface — it resolves to a path and
 * hands it to the caller, which owns the actual cwd change (`changeSessionCwd`
 * / `setNewChatWorkspaceTarget`). Mirrors the session picker's cmdk surface so
 * both overlays feel like one family.
 */
export function ProjectSwitcherDialog({
  activeCwd,
  onOpenChange,
  onOpenFolder,
  onSelect,
  open
}: ProjectSwitcherDialogProps) {
  const { t } = useI18n()
  const copy = t.projectSwitcher
  const rows = useStore($recentProjectRows)
  const [search, setSearch] = useState('')

  useEffect(() => {
    if (!open) {
      setSearch('')

      return
    }

    // Re-probe on every open: a folder can be deleted or a volume unmounted
    // between visits, and a returning volume must be able to un-grey its row.
    void probeRecentProjects()
  }, [open])

  return (
    <DialogPrimitive.Root onOpenChange={onOpenChange} open={open}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-[200] bg-black/15 backdrop-blur-[1px] data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-[14vh] z-[210] w-[min(40rem,calc(100vw-2rem))] -translate-x-1/2 overflow-hidden rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-chat-bubble-background) shadow-lg duration-150 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:slide-in-from-top-2 data-[state=open]:zoom-in-95"
        >
          <DialogPrimitive.Title className="sr-only">{copy.title}</DialogPrimitive.Title>
          <Command className="bg-transparent" loop>
            <CommandInput onValueChange={setSearch} placeholder={copy.searchPlaceholder} value={search} />
            <CommandList className="max-h-[min(24rem,60vh)]">
              <CommandEmpty>{copy.empty}</CommandEmpty>
              <CommandGroup
                className="**:[[cmdk-group-heading]]:uppercase **:[[cmdk-group-heading]]:tracking-wider **:[[cmdk-group-heading]]:text-[0.6875rem] **:[[cmdk-group-heading]]:text-muted-foreground/70"
                heading={copy.recentHeading}
              >
                {rows.map(row => {
                  const label = projectPathLabel(row.path)
                  const isActive = row.path === activeCwd

                  return (
                    <CommandItem
                      className="gap-2.5"
                      key={row.path}
                      onSelect={() => {
                        // A folder we've already proven gone must never
                        // re-anchor a session's tools at a dead path.
                        if (row.missing) {
                          return
                        }

                        onSelect(row.path)
                        onOpenChange(false)
                      }}
                      value={`${label} ${row.path}`}
                    >
                      <FolderOpen
                        className={cn('size-4 shrink-0 text-muted-foreground', row.missing && 'opacity-50')}
                      />
                      <span className="flex min-w-0 flex-col leading-snug">
                        <span className={cn('truncate', row.missing && 'text-muted-foreground line-through')}>
                          {label}
                        </span>
                        <span className="truncate text-xs text-muted-foreground/70">
                          {row.missing ? copy.missingHint : row.path}
                        </span>
                      </span>
                      {row.missing && (
                        <span className="ml-auto inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
                          <AlertTriangle className="size-3.5" />
                          {copy.missingBadge}
                        </span>
                      )}
                      {isActive && !row.missing && (
                        <span className="ml-auto size-1.5 shrink-0 rounded-full bg-foreground/70" />
                      )}
                      <button
                        aria-label={copy.remove}
                        className={cn(
                          'shrink-0 rounded p-1 text-muted-foreground/60 hover:text-foreground',
                          !row.missing && !isActive && 'ml-auto'
                        )}
                        onClick={event => {
                          // The row owns Enter/click; removing must not also switch.
                          event.stopPropagation()
                          forgetRecentProject(row.path)
                        }}
                        title={copy.remove}
                        type="button"
                      >
                        <Trash2 className="size-3.5" />
                      </button>
                    </CommandItem>
                  )
                })}
              </CommandGroup>
              <CommandGroup>
                <CommandItem
                  className="gap-2.5"
                  onSelect={() => {
                    onOpenChange(false)
                    onOpenFolder()
                  }}
                  value={copy.openFolder}
                >
                  <FolderOpen className="size-4 shrink-0 text-muted-foreground" />
                  <span className="truncate">{copy.openFolder}</span>
                </CommandItem>
              </CommandGroup>
            </CommandList>
          </Command>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}
