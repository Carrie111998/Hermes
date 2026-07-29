import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { enterProjectWorkspace } from '@/store/projects'

import { enterArtifactProject } from './project-context'

vi.mock('@/store/projects', () => ({
  enterProjectWorkspace: vi.fn()
}))

const enterWorkspace = vi.mocked(enterProjectWorkspace)

const projects: SidebarProjectTree[] = [
  {
    id: 'p_desktop',
    label: 'Hermes Desktop',
    path: '/work/hermes/apps/desktop',
    repos: [],
    sessionCount: 0
  }
]

describe('enterArtifactProject', () => {
  beforeEach(() => {
    enterWorkspace.mockClear()
  })

  it('enters a concrete project instead of applying a page-local filter', () => {
    expect(enterArtifactProject('p_desktop', projects)).toBe(true)
    expect(enterWorkspace).toHaveBeenCalledOnce()
    expect(enterWorkspace).toHaveBeenCalledWith(projects[0])
  })

  it('leaves the workspace unchanged for the all-projects browser option', () => {
    expect(enterArtifactProject('__all_artifact_projects__', projects)).toBe(false)
    expect(enterWorkspace).not.toHaveBeenCalled()
  })
})
