/**
 * Project list/tree/scope atoms with no gateway dependency.
 *
 * Profile-switch cleanup and unit tests that mock only part of `@/store/gateway`
 * can touch this module without loading the projects RPC client (#79406).
 */
import { atom } from 'nanostores'

import { type SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { persistentAtom } from '@/lib/persisted'
import type { ProjectInfo } from '@/types/hermes'

export const $projects = atom<ProjectInfo[]>([])
export const $activeProjectId = atom<null | string>(null)
export const $projectTree = atom<SidebarProjectTree[]>([])
export const $projectTreeLoading = atom(false)

export const ALL_PROJECTS = '__all_projects__'

const PROJECT_SCOPE_KEY = 'hermes.desktop.projectScope'

export const $projectScope = persistentAtom<string>(PROJECT_SCOPE_KEY, ALL_PROJECTS, {
  decode: raw => raw || ALL_PROJECTS,
  encode: value => value || ALL_PROJECTS
})

export function exitProjectScope(): void {
  $projectScope.set(ALL_PROJECTS)
}

/** Drop the previous profile's projects.db snapshot on profile switch (#79406). */
export function clearProjectsCacheForProfileSwitch(): void {
  $projects.set([])
  $projectTree.set([])
  $activeProjectId.set(null)
  exitProjectScope()
}
