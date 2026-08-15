import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'

import { SidebarFilterMenu } from './filter-menu'
import { NewSessionHeaderButton } from './new-session-header-button'
import { ProjectMenu, type SidebarProjectTree, StartWorkButton } from './projects'

// Section-header action icons stay hidden until the whole header row is hovered
// (group/section lives on SidebarSectionHeader), mirroring the artifacts/file
// browser header affordances. focus-visible keeps them keyboard-reachable.
const HEADER_ACTION_BTN =
  'text-(--ui-text-tertiary) opacity-0 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground group-hover/section:opacity-100 focus-visible:opacity-100'

// The view toggle (overview group toggle / in-project back) is the one control
// that stays visible at all times — it's the stable navigation affordance, not
// a hover-revealed action.
const HEADER_NAV_BTN =
  'text-(--ui-text-tertiary) opacity-70 transition-opacity hover:bg-(--ui-control-hover-background) hover:text-foreground hover:opacity-100 focus-visible:opacity-100'

interface SessionsHeaderActionProps {
  activeProjectId: null | string
  agentsGrouped: boolean
  enteredProject?: SidebarProjectTree
  inProject: boolean
  labels: {
    newProject: string
    newSession: string
    showProjects: string
  }
  onExitProjectScope: () => void
  onNewSessionInWorkspace: (path: null | string) => void
  onOpenProjectCreate: () => void
  showAllProfiles: boolean
}

/** The Sessions section header's trailing action(s): a "+" to start a new
 *  session (or project, when grouped), plus the filter menu at the overview
 *  / "back to projects" and project-menu controls once a project is entered.
 *  Home is a synthetic entered "project" with no folder of its own — it still
 *  needs the "+" (#83479), just not the rename/theme/delete `ProjectMenu`. */
export function SessionsHeaderAction({
  activeProjectId,
  agentsGrouped,
  enteredProject,
  inProject,
  labels,
  onExitProjectScope,
  onNewSessionInWorkspace,
  onOpenProjectCreate,
  showAllProfiles
}: SessionsHeaderActionProps) {
  if (inProject && enteredProject) {
    return (
      <div className="group/workspace flex shrink-0 items-center gap-0.5">
        {enteredProject.path && <StartWorkButton repoPath={enteredProject.path} />}
        {/* Home has no folder and no record to rename, theme, or delete. */}
        {enteredProject.isNoProject ? (
          <NewSessionHeaderButton
            className={HEADER_ACTION_BTN}
            label={labels.newSession}
            onClick={() => onNewSessionInWorkspace(null)}
          />
        ) : (
          <ProjectMenu
            isActive={enteredProject.id === activeProjectId}
            onExitScope={onExitProjectScope}
            project={enteredProject}
            scoped
          />
        )}
        <div className="grid size-6 place-items-center">
          <Tip label={labels.showProjects}>
            <Button
              aria-label={labels.showProjects}
              className={HEADER_NAV_BTN}
              onClick={event => {
                event.stopPropagation()
                onExitProjectScope()
              }}
              size="icon-xs"
              variant="ghost"
            >
              <Codicon name="list-unordered" size="0.75rem" />
            </Button>
          </Tip>
        </div>
      </div>
    )
  }

  return (
    <div className="flex shrink-0 items-center gap-0.5">
      {!showAllProfiles ? (
        <NewSessionHeaderButton
          className={HEADER_ACTION_BTN}
          label={agentsGrouped ? labels.newProject : labels.newSession}
          onClick={() => (agentsGrouped ? onOpenProjectCreate() : onNewSessionInWorkspace(null))}
        />
      ) : null}
      <div className="grid size-6 place-items-center">
        <SidebarFilterMenu className={HEADER_NAV_BTN} />
      </div>
    </div>
  )
}
