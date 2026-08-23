import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useI18n } from '@/i18n'
import { notifyError } from '@/store/notifications'

import type { ProjectsGroupingContribution, ProjectsGroupingSnapshot } from './projects-presentation'

export function normalizeProjectGroupName(value: string): string {
  return value.trim().replace(/\s+/g, ' ')
}

export function ProjectGroupDialog({
  contribution,
  snapshot,
  open,
  onOpenChange
}: {
  contribution: ProjectsGroupingContribution
  snapshot: ProjectsGroupingSnapshot
  open: boolean
  onOpenChange(open: boolean): void
}) {
  const { t } = useI18n()
  const p = t.sidebar.projects
  const [name, setName] = useState('')
  const [pending, setPending] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const normalized = normalizeProjectGroupName(name)
  const duplicate = snapshot.groups.some(
    group => normalizeProjectGroupName(group.label).toLocaleLowerCase() === normalized.toLocaleLowerCase()
  )
  const invalid = normalized.length < 1 || normalized.length > 100

  useEffect(() => {
    if (open) {
      setName('')
      setPending(false)
      window.setTimeout(() => inputRef.current?.focus(), 0)
    }
  }, [open])

  const submit = async () => {
    if (pending || invalid || duplicate || !contribution.createGroup) return
    setPending(true)
    try {
      await contribution.createGroup(normalized)
      onOpenChange(false)
    } catch (error) {
      notifyError(error, p.groupCreateFailed)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{p.createGroupTitle}</DialogTitle>
        </DialogHeader>
        <Input
          aria-invalid={Boolean(name) && (invalid || duplicate)}
          disabled={pending}
          onChange={event => setName(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') {
              event.preventDefault()
              void submit()
            }
          }}
          placeholder={p.groupNamePlaceholder}
          ref={inputRef}
          value={name}
        />
        {name && (invalid || duplicate) ? (
          <p className="text-xs text-destructive">{duplicate ? p.groupNameDuplicate : p.groupNameInvalid}</p>
        ) : null}
        <DialogFooter>
          <Button disabled={pending} onClick={() => onOpenChange(false)} variant="ghost">
            {t.common.cancel}
          </Button>
          <Button disabled={pending || invalid || duplicate} onClick={() => void submit()}>
            {p.create}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
