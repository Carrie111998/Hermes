import { useState } from 'react'

import { ActionsMenu, renderActionItem } from '@/components/ui/actions-menu'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'

import type { ProjectGroupDeleteProject } from './project-group-delete'
import { ProjectGroupDeleteDialog } from './project-group-delete-dialog'
import type { ProjectGroupDescriptor, ProjectsGroupingContribution } from './projects-presentation'

export function ProjectGroupActions({
  contribution,
  group,
  projects
}: {
  contribution: ProjectsGroupingContribution
  group: ProjectGroupDescriptor
  projects: readonly ProjectGroupDeleteProject[]
}) {
  const { t } = useI18n()
  const p = t.sidebar.projects
  const [deleteOpen, setDeleteOpen] = useState(false)

  if (!contribution.deleteGroup) {
    return null
  }

  return (
    <>
      <ActionsMenu
        ariaLabel={p.menu}
        contentClassName="w-40"
        items={kit =>
          renderActionItem(kit, {
            icon: 'trash',
            key: 'delete-group',
            label: p.deleteGroup,
            onSelect: () => setDeleteOpen(true),
            variant: 'destructive'
          })
        }
      >
        <button
          aria-label={p.menu}
          className="grid size-4 shrink-0 place-items-center rounded-sm bg-transparent text-(--ui-text-quaternary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/project-group:opacity-100 data-[state=open]:opacity-100"
          type="button"
        >
          <Codicon name="kebab-vertical" size="0.75rem" />
        </button>
      </ActionsMenu>
      <ProjectGroupDeleteDialog
        contribution={contribution}
        group={group}
        onOpenChange={setDeleteOpen}
        open={deleteOpen}
        projects={projects}
      />
    </>
  )
}
