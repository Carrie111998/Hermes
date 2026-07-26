import { atom } from 'nanostores'

import { readDesktopDir } from '@/lib/desktop-fs'
import { normalizeProjectPath } from '@/lib/project-paths'

import { $missingProjectPaths, $recentProjects, markProjectMissing } from './recent-projects'

/** Whether the project switcher dialog is open. */
export const $projectSwitcherOpen = atom(false)

export const openProjectSwitcher = (): void => $projectSwitcherOpen.set(true)
export const setProjectSwitcherOpen = (open: boolean): void => $projectSwitcherOpen.set(open)

/**
 * Probe one workspace for existence and cache the verdict.
 *
 * `readDesktopDir` reports a missing/unreadable directory as an `error` code
 * (ENOENT for a deleted or unmounted folder) rather than throwing, and it is
 * remote-aware, so this works against a remote gateway's filesystem too.
 * Returns true when the directory is readable.
 */
export async function probeProjectExists(path: string): Promise<boolean> {
  const normalized = normalizeProjectPath(path)

  if (!normalized) {
    return false
  }

  try {
    const { error } = await readDesktopDir(normalized)
    const exists = !error
    markProjectMissing(normalized, !exists)

    return exists
  } catch {
    // A transport failure is NOT proof the folder is gone; leave the cached
    // verdict alone so a flaky probe can't grey out a healthy project.
    return !$missingProjectPaths.get().includes(normalized)
  }
}

/** Probe every remembered workspace (bounded by MAX_RECENT_PROJECTS). */
export async function probeRecentProjects(): Promise<void> {
  await Promise.all($recentProjects.get().map(entry => probeProjectExists(entry.path)))
}
