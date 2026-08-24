import { useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { ErrorBanner } from '@/components/ui/error-state'
import { useI18n } from '@/i18n'
import { refreshProjects, refreshProjectTree, renameProjectsMany } from '@/store/projects'

import {
  buildProjectGroupRenamePlan,
  deleteProjectGroup,
  ProjectGroupDeleteCompensationError,
  type ProjectGroupDeleteProject,
  ProjectGroupDeleteValidationError
} from './project-group-delete'
import type { ProjectGroupDescriptor, ProjectsGroupingContribution } from './projects-presentation'

function createOperationId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `delete-group-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function uniqueMemberIds(group: ProjectGroupDescriptor): string[] {
  return [...new Set(group.projectIds.map(id => id.trim()).filter(Boolean))]
}

export function ProjectGroupDeleteDialog({
  contribution,
  group,
  open,
  onOpenChange,
  projects
}: {
  contribution: ProjectsGroupingContribution
  group: ProjectGroupDescriptor
  open: boolean
  onOpenChange(open: boolean): void
  projects: readonly ProjectGroupDeleteProject[]
}) {
  const { t } = useI18n()
  const p = t.sidebar.projects
  const [prependGroupName, setPrependGroupName] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [operationId, setOperationId] = useState(createOperationId)
  const deleteButtonRef = useRef<HTMLButtonElement>(null)
  const memberIds = useMemo(() => uniqueMemberIds(group), [group])
  const projectsById = useMemo(() => new Map(projects.map(project => [project.id, project])), [projects])
  const memberProjects = memberIds.map(id => projectsById.get(id)).filter(Boolean) as ProjectGroupDeleteProject[]
  const missingMember = memberProjects.length !== memberIds.length

  const renamePlan = useMemo(() => {
    if (!prependGroupName || missingMember) {
      return null
    }

    try {
      return buildProjectGroupRenamePlan(group, projects)
    } catch (reason) {
      return reason
    }
  }, [group, missingMember, prependGroupName, projects])

  const validationError = missingMember
    ? new ProjectGroupDeleteValidationError('stale', 'A Project is missing from the deletion review')
    : renamePlan instanceof ProjectGroupDeleteValidationError
      ? renamePlan
      : null

  useEffect(() => {
    if (!open) {
      return
    }

    setPrependGroupName(false)
    setPending(false)
    setError(null)
    setOperationId(createOperationId())
  }, [open, group.id])

  const errorMessage = (reason: unknown): string => {
    if (reason instanceof ProjectGroupDeleteCompensationError) {
      return p.deleteGroupCompensationFailed(reason.affectedProjects.map(project => project.name).join(', '))
    }

    if (reason instanceof ProjectGroupDeleteValidationError) {
      return reason.reason === 'collision' ? p.deleteGroupCollision : p.deleteGroupStale
    }

    return reason instanceof Error && reason.message ? reason.message : p.deleteGroupFailed
  }

  const performDelete = async (prepend: boolean) => {
    await deleteProjectGroup({
      contribution,
      group,
      operationId,
      prependGroupName: prepend,
      projects,
      reconcile: async () => {
        await Promise.all([refreshProjects(), refreshProjectTree()])
      },
      renameMany: renameProjectsMany
    })
  }

  if (memberIds.length === 0) {
    return (
      <ConfirmDialog
        confirmLabel={p.deleteGroup}
        description={p.deleteGroupEmptyDescription}
        destructive
        onClose={() => onOpenChange(false)}
        onConfirm={async () => {
          try {
            await performDelete(false)
          } catch (reason) {
            throw new Error(errorMessage(reason))
          }
        }}
        open={open}
        title={p.deleteGroupTitle(group.label.trim())}
      />
    )
  }

  const submit = async () => {
    if (pending || validationError) {
      return
    }

    setPending(true)
    setError(null)

    try {
      await performDelete(prependGroupName)
      onOpenChange(false)
    } catch (reason) {
      setError(errorMessage(reason))
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog onOpenChange={value => !pending && onOpenChange(value)} open={open}>
      <DialogContent
        className="max-w-3xl"
        onKeyDown={event => {
          if (event.key === 'Enter' && !pending) {
            event.preventDefault()
            void submit()
          }
        }}
        onOpenAutoFocus={event => {
          event.preventDefault()
          deleteButtonRef.current?.focus()
        }}
      >
        <DialogHeader>
          <DialogTitle>{p.deleteGroupTitle(group.label.trim())}</DialogTitle>
          <DialogDescription>{p.deleteGroupMoveDescription}</DialogDescription>
        </DialogHeader>

        <label className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={prependGroupName}
            disabled={pending}
            onCheckedChange={checked => setPrependGroupName(checked === true)}
          />
          <span>{p.deleteGroupPrepend}</span>
        </label>

        {prependGroupName && Array.isArray(renamePlan) ? (
          <div aria-label={p.deleteGroupPreviewLabel} className="grid min-h-0 grid-cols-2 gap-4" role="group">
            <section aria-labelledby="project-group-preview-before">
              <h3 className="mb-2 text-xs font-semibold text-(--ui-text-secondary)" id="project-group-preview-before">
                {p.deleteGroupPreviewBefore}
              </h3>
              <ul aria-label={p.deleteGroupPreviewBefore} className="text-sm" role="tree">
                <li aria-expanded="true" role="treeitem">
                  <span className="font-medium">{group.label.trim()}</span>
                  <ul className="ml-4 mt-1 grid gap-1" role="group">
                    {memberProjects.map(project => (
                      <li key={project.id} role="treeitem">
                        {project.name.trim()}
                      </li>
                    ))}
                  </ul>
                </li>
              </ul>
            </section>
            <section
              aria-labelledby="project-group-preview-after"
              className="border-l border-(--ui-stroke-tertiary) pl-4"
            >
              <h3 className="mb-2 text-xs font-semibold text-(--ui-text-secondary)" id="project-group-preview-after">
                {p.deleteGroupPreviewAfter}
              </h3>
              <ul aria-label={p.deleteGroupPreviewAfter} className="grid gap-1 text-sm">
                {renamePlan.map(rename => (
                  <li key={rename.id}>{rename.newName}</li>
                ))}
              </ul>
            </section>
          </div>
        ) : null}

        {(error || validationError) && (
          <div role="alert">
            <ErrorBanner>
              {error ?? (validationError?.reason === 'collision' ? p.deleteGroupCollision : p.deleteGroupStale)}
            </ErrorBanner>
          </div>
        )}

        <DialogFooter>
          <Button disabled={pending} onClick={() => onOpenChange(false)} type="button" variant="ghost">
            {t.common.cancel}
          </Button>
          <Button
            disabled={pending || Boolean(validationError)}
            onClick={() => void submit()}
            ref={deleteButtonRef}
            type="button"
            variant="destructive"
          >
            {pending ? p.deleteGroupPending : p.deleteGroup}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
