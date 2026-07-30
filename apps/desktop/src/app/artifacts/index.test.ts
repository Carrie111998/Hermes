import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $projectScope } from '@/store/projects'
import { $connection } from '@/store/session'
import type { SessionInfo, SessionMessage } from '@/types/hermes'

import type { SidebarProjectTree } from '../chat/sidebar/projects/workspace-groups'

import {
  artifactImageSrc,
  artifactSessionsForProject,
  collectArtifactsForSession,
  preferredArtifactProjectId
} from './artifact-utils'

import { enterArtifactProject, useProjectArtifacts } from './index'

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: 'session-1',
    input_tokens: 0,
    is_active: false,
    last_active: 1000,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    source: null,
    started_at: 1000,
    title: 'Session',
    tool_call_count: 0,
    ...overrides
  }
}

describe('collectArtifactsForSession', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
    $connection.set(null)
  })

  it('indexes plain https links from assistant text', () => {
    const artifacts = collectArtifactsForSession(makeSession(), [
      {
        content: 'Reference: https://example.com/docs/getting-started',
        role: 'assistant',
        timestamp: 2000
      }
    ])

    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({
      href: 'https://example.com/docs/getting-started',
      kind: 'link',
      value: 'https://example.com/docs/getting-started'
    })
  })

  it('indexes http links present in tool JSON payloads', () => {
    const messages: SessionMessage[] = [
      {
        content: JSON.stringify({ source_url: 'https://example.com/changelog/latest' }),
        role: 'tool',
        timestamp: 3000
      }
    ]

    const artifacts = collectArtifactsForSession(makeSession({ id: 'session-2' }), messages)

    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({
      href: 'https://example.com/changelog/latest',
      kind: 'link',
      value: 'https://example.com/changelog/latest'
    })
  })

  it('resolves remote image artifact thumbnails through the desktop fs bridge', async () => {
    const api = vi.fn(async ({ path }: { path: string }) => {
      if (path.startsWith('/api/fs/read-data-url?')) {
        return { dataUrl: 'data:image/jpeg;base64,cmVtb3Rl' }
      }

      throw new Error(`unexpected path ${path}`)
    })

    vi.stubGlobal('window', { hermesDesktop: { api } })
    $connection.set({ baseUrl: 'https://gw', mode: 'remote', token: 'secret' } as never)

    const path = '/Users/me/.hermes/skills/work-esab/references/images/manual-step03.jpeg'
    const downloadHref = `https://gw/api/files/download?path=${encodeURIComponent(path)}&token=secret`

    await expect(artifactImageSrc(path, downloadHref)).resolves.toBe('data:image/jpeg;base64,cmVtb3Rl')

    expect(api).toHaveBeenCalledWith({
      path: '/api/fs/read-data-url?path=%2FUsers%2Fme%2F.hermes%2Fskills%2Fwork-esab%2Freferences%2Fimages%2Fmanual-step03.jpeg'
    })
  })
})

describe('project artifact model', () => {
  const project = (overrides: Partial<SidebarProjectTree> = {}): SidebarProjectTree => ({
    id: 'project-1',
    label: 'Hermes',
    path: '/work/hermes',
    repos: [],
    sessionCount: 0,
    previewSessions: [],
    ...overrides
  })

  it('uses backend-provided project sessions and deduplicates overview previews', () => {
    const recent = makeSession({ id: 'recent', last_active: 30 })
    const older = makeSession({ id: 'older', last_active: 10 })

    const sessions = artifactSessionsForProject(
      project({
        previewSessions: [recent],
        repos: [
          {
            groups: [{ id: 'main', label: 'main', path: '/work/hermes', sessions: [older, recent] }],
            id: 'repo',
            label: 'hermes',
            path: '/work/hermes',
            sessionCount: 2
          }
        ]
      })
    )

    expect(sessions.map(session => session.id)).toEqual(['recent', 'older'])
  })

  it('prefers explicit project scope and otherwise resolves the current authoritative path', () => {
    const projects = [project(), project({ id: 'project-2', label: 'Desktop', path: '/work/hermes/apps/desktop' })]

    expect(
      preferredArtifactProjectId({
        currentCwd: '/work/hermes/apps/desktop/src',
        projectScope: '__all_projects__',
        projects
      })
    ).toBe('project-2')
    expect(
      preferredArtifactProjectId({
        currentCwd: '/work/hermes/apps/desktop/src',
        projectScope: 'project-1',
        projects
      })
    ).toBe('project-1')
  })

  it('matches Windows project paths without falling back to the first project', () => {
    const projects = [
      project({ id: 'project-1', path: 'C:\\work\\hermes' }),
      project({ id: 'project-2', path: 'C:\\work\\hermes\\apps\\desktop' })
    ]

    expect(
      preferredArtifactProjectId({
        currentCwd: 'C:\\work\\hermes\\apps\\desktop\\src',
        projects
      })
    ).toBe('project-2')
  })
})

describe('project artifact interactions', () => {
  const project: SidebarProjectTree = {
    id: 'p_project',
    label: 'Hermes',
    path: '/work/hermes',
    repos: [],
    sessionCount: 1,
    previewSessions: [makeSession()]
  }

  beforeEach(() => {
    $projectScope.set('__all_projects__')
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('enters a selected project but leaves the all-projects view as a library-only scope', () => {
    enterArtifactProject('/work/hermes')
    enterArtifactProject('__all_artifact_projects__')

    expect($projectScope.get()).toBe('/work/hermes')
  })

  it('reloads an expanded project when the all-projects refresh generation changes', async () => {
    const loader = vi.fn().mockResolvedValue({
      artifacts: [],
      loadedSessions: 1,
      sessions: project.previewSessions
    })

    const { rerender } = renderHook(
      ({ refreshGeneration }) => useProjectArtifacts(project, 8, refreshGeneration, loader),
      { initialProps: { refreshGeneration: 0 } }
    )

    await waitFor(() => expect(loader).toHaveBeenCalledOnce())

    rerender({ refreshGeneration: 1 })

    await waitFor(() => expect(loader).toHaveBeenCalledTimes(2))
  })
})
