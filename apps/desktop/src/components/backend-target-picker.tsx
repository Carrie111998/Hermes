import * as React from 'react'

import { Button } from '@/components/ui/button'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'

// One pickable backend target. The id is opaque to this component — it is
// handed straight back through `onSelect` and never interpreted. No URLs,
// tokens, connection descriptors, profiles, or modes belong in here: callers
// keep that knowledge behind the id.
export interface BackendTargetChoice {
  id: string
  label: string
  description?: string
}

// Every string the picker shows, injected by the caller. The component owns
// no text of its own, so adding it needs no i18n/locale changes.
export interface BackendTargetPickerCopy {
  title: string
  description?: string
  searchPlaceholder: string
  currentBadge: string
  emptyLabel: string
  noMatchLabel: string
  cancelLabel: string
}

export interface BackendTargetPickerDialogProps {
  open: boolean
  choices: BackendTargetChoice[]
  currentChoiceId: string | null
  copy: BackendTargetPickerCopy
  onSelect: (choiceId: string) => void
  onOpenChange: (open: boolean) => void
}

// Dependency-injected picker dialog: the caller supplies the choices and all
// copy; the component filters locally, reports only the opaque id, and asks
// to close. It renders nothing while `open` is false.
export function BackendTargetPickerDialog({
  choices,
  copy,
  currentChoiceId,
  onOpenChange,
  onSelect,
  open
}: BackendTargetPickerDialogProps) {
  const [query, setQuery] = React.useState('')

  // Reopening the dialog always starts from the full list.
  React.useEffect(() => {
    if (open) {
      setQuery('')
    }
  }, [open])

  const normalizedQuery = query.trim().toLowerCase()

  const visibleChoices = React.useMemo(
    () =>
      normalizedQuery
        ? choices.filter(
            choice =>
              choice.label.toLowerCase().includes(normalizedQuery) ||
              (choice.description ?? '').toLowerCase().includes(normalizedQuery)
          )
        : choices,
    [choices, normalizedQuery]
  )

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{copy.title}</DialogTitle>
          {copy.description ? <DialogDescription>{copy.description}</DialogDescription> : null}
        </DialogHeader>

        {choices.length === 0 ? (
          <p className="py-6 text-center text-sm text-muted-foreground" data-slot="backend-target-picker-empty">
            {copy.emptyLabel}
          </p>
        ) : (
          <Command
            className="rounded-md border border-border"
            // Filtering already happened above against label + description;
            // keep cmdk from second-guessing it.
            filter={() => 1}
          >
            <CommandInput autoFocus onValueChange={setQuery} placeholder={copy.searchPlaceholder} value={query} />
            <CommandList className="max-h-64">
              <CommandEmpty>{copy.noMatchLabel}</CommandEmpty>
              <CommandGroup>
                {visibleChoices.map(choice => {
                  const isCurrent = choice.id === currentChoiceId

                  return (
                    <CommandItem
                      aria-current={isCurrent || undefined}
                      data-current={isCurrent ? 'true' : undefined}
                      key={choice.id}
                      onSelect={() => {
                        onSelect(choice.id)
                      }}
                      value={choice.id}
                    >
                      <div className="flex min-w-0 flex-col gap-0.5">
                        <span className="truncate">{choice.label}</span>
                        {choice.description ? (
                          <span className="truncate text-xs text-muted-foreground">{choice.description}</span>
                        ) : null}
                      </div>
                      {isCurrent ? (
                        <span
                          className="ml-auto shrink-0 text-xs text-(--ui-accent)"
                          data-slot="backend-target-picker-current-badge"
                        >
                          {copy.currentBadge}
                        </span>
                      ) : null}
                    </CommandItem>
                  )
                })}
              </CommandGroup>
            </CommandList>
          </Command>
        )}

        <DialogFooter>
          <Button onClick={() => void onOpenChange(false)} variant="secondary">
            {copy.cancelLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
