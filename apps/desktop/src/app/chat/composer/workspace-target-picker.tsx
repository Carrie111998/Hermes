import { useStore } from '@nanostores/react'

import { baseName, NO_PROJECT_ID, type SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { Codicon } from '@/components/ui/codicon'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { useI18n } from '@/i18n'
import { $projectTree, projectIdForCwd } from '@/store/projects'
import { setCurrentBranch, setCurrentCwd, setCurrentCwdTransient, setNewChatWorkspaceTarget } from '@/store/session'

interface WorkspaceTargetPickerProps {
  cwd: string
  hasMessages?: boolean
  sessionId?: null | string
}

const projectRootCwd = (project: SidebarProjectTree): string =>
  (project.path || project.repos.find(repo => repo.path)?.path || '').trim()

export function WorkspaceTargetPicker({ cwd, hasMessages = false, sessionId = null }: WorkspaceTargetPickerProps) {
  const { t } = useI18n()
  const projects = useStore($projectTree)

  // This is a one-shot draft control, never a live-session cwd picker. Besides
  // avoiding surprise retargets, the message guard covers the short interval in
  // which first-submit UI can exist before the runtime id is published.
  if (sessionId || hasMessages) {
    return null
  }

  const target = cwd.trim()
  const selectedProjectId = target ? projectIdForCwd(target) : NO_PROJECT_ID
  const selectedProject = projects.find(project => project.id === selectedProjectId)
  const label = selectedProject?.label || (target ? baseName(target) || target : t.sidebar.projects.home)
  const choices = projects.filter(project => !project.archived && !project.isNoProject && projectRootCwd(project))

  const selectTarget = (next: null | string) => {
    setCurrentBranch('')

    if (next === null) {
      // Home is deliberately transient: detaching this draft must not erase the
      // remembered remote/default cwd used by later resolution fallbacks.
      setCurrentCwdTransient('')
      setNewChatWorkspaceTarget(null)

      return
    }

    setCurrentCwd(next)
    setNewChatWorkspaceTarget(next)
  }

  return (
    <div
      className="flex min-w-0 items-center text-[0.6875rem] text-(--ui-text-tertiary)"
      data-slot="workspace-target-picker"
    >
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            aria-label={`${t.sidebar.projects.sectionLabel}: ${label}`}
            className="flex min-w-0 items-center gap-1 bg-transparent text-left transition-colors hover:text-foreground"
            type="button"
          >
            <Codicon name={selectedProject?.icon || (target ? 'folder-library' : 'home')} size="0.75rem" />
            <span className="max-w-48 truncate">{label}</span>
            <Codicon className="text-(--ui-text-quaternary)" name="chevron-down" size="0.75rem" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="min-w-48">
          <DropdownMenuRadioGroup value={selectedProjectId || target}>
            <DropdownMenuRadioItem onSelect={() => selectTarget(null)} value={NO_PROJECT_ID}>
              <Codicon name="home" size="0.75rem" />
              <span className="min-w-0 flex-1 truncate">{t.sidebar.projects.home}</span>
            </DropdownMenuRadioItem>
            {choices.map(project => {
              const path = projectRootCwd(project)

              return (
                <DropdownMenuRadioItem key={project.id} onSelect={() => selectTarget(path)} value={project.id}>
                  <Codicon name={project.icon || 'folder-library'} size="0.75rem" />
                  <span className="min-w-0 flex-1 truncate">{project.label}</span>
                </DropdownMenuRadioItem>
              )
            })}
          </DropdownMenuRadioGroup>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )
}
