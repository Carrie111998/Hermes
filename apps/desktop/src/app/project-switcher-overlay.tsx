import { useStore } from '@nanostores/react'
import type { MutableRefObject } from 'react'
import { useCallback } from 'react'

import { ProjectSwitcherDialog } from '@/components/project-switcher'
import { useI18n } from '@/i18n'
import { projectPathLabel } from '@/lib/project-paths'
import { notify, notifyError } from '@/store/notifications'
import { $projectSwitcherOpen, setProjectSwitcherOpen } from '@/store/project-switcher'
import { pickProjectFolder } from '@/store/projects'
import { $currentCwd } from '@/store/session'

import { switchToProject } from './session/switch-project'

interface ProjectSwitcherOverlayProps {
  /** Live pointer to the focused session, threaded straight through to the
   *  switch action so it re-reads the CURRENT anchor after every await. */
  activeSessionIdRef: MutableRefObject<string | null>
  changeSessionCwd: (cwd: string) => Promise<void>
}

/**
 * Mounts the recent-projects switcher. Owns only presentation + notification;
 * the cwd mutation belongs to `changeSessionCwd` (the existing per-session /
 * new-chat path), and the guard rails belong to `switchToProject`.
 */
export function ProjectSwitcherOverlay({ activeSessionIdRef, changeSessionCwd }: ProjectSwitcherOverlayProps) {
  const { t } = useI18n()
  const copy = t.projectSwitcher
  const open = useStore($projectSwitcherOpen)
  const currentCwd = useStore($currentCwd)

  const applyProject = useCallback(
    async (path: string) => {
      try {
        const result = await switchToProject({ activeSessionIdRef, changeSessionCwd, path })

        if (result === 'missing') {
          notify({ kind: 'warning', message: copy.missingHint, title: copy.missingBadge })

          return
        }

        if (result === 'switched') {
          notify({ kind: 'success', message: copy.switchedTo(projectPathLabel(path)) })
        }

        // 'session-changed' / 'invalid' are silent: the user's focus moved on,
        // so a toast about the abandoned intent would be noise.
      } catch (err) {
        notifyError(err, copy.switchFailed)
      }
    },
    [activeSessionIdRef, changeSessionCwd, copy]
  )

  const handleOpenFolder = useCallback(() => {
    void pickProjectFolder().then(dir => (dir ? applyProject(dir) : undefined))
  }, [applyProject])

  return (
    <ProjectSwitcherDialog
      activeCwd={currentCwd}
      onOpenChange={setProjectSwitcherOpen}
      onOpenFolder={handleOpenFolder}
      onSelect={path => void applyProject(path)}
      open={open}
    />
  )
}
