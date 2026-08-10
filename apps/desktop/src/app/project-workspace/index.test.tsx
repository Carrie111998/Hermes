// @vitest-environment jsdom
import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { ProjectWorkspaceContent, type WorkspaceSession } from './index'

const project = {
  id: 'p_kiwi',
  label: '키위스튜디오 작업실',
  path: '/Users/kiwistudio/projects/kiwi',
  repoCount: 2
}

const sessions: WorkspaceSession[] = [
  {
    id: 'session-1',
    title: '대시보드 스타일 개선',
    preview: 'Codex형 프로젝트 작업실을 Hermes에 구현한다.',
    active: true,
    busy: false,
    source: 'slack'
  },
  {
    id: 'session-2',
    title: 'CLI 로컬 검토',
    preview: '로컬 변경사항을 확인한다.',
    active: false,
    busy: false,
    source: 'cli'
  }
]

describe('ProjectWorkspaceContent', () => {
  it('renders the project workspace hierarchy and truthful context states', () => {
    render(
      <MemoryRouter>
        <ProjectWorkspaceContent
          branch="feat/kiwi-project-workspace"
          changedFiles={2}
          cwd={project.path}
          notionConnected
          onCreateProject={vi.fn()}
          onOpenSession={vi.fn()}
          onSelectProject={vi.fn()}
          onStartTask={vi.fn()}
          project={project}
          projects={[project]}
          sessions={sessions}
        />
      </MemoryRouter>
    )

    expect(screen.getByRole('heading', { level: 2, name: '키위스튜디오 작업실' })).toBeTruthy()
    expect(screen.getByText('Notion context available')).toBeTruthy()
    expect(screen.getAllByText('feat/kiwi-project-workspace').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('대시보드 스타일 개선')).toBeTruthy()
    expect(screen.getAllByText('Slack').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByRole('button', { name: 'Slack' })).toBeTruthy()
    expect(screen.getByText('CLI 로컬 검토')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Slack' }))
    expect(screen.getByText('대시보드 스타일 개선')).toBeTruthy()
    expect(screen.queryByText('CLI 로컬 검토')).toBeNull()
    expect(screen.getByText('2 changed files')).toBeTruthy()
  })

  it('sends coding tasks separately from read-only Notion and Slack research', () => {
    const onStartTask = vi.fn()
    const onSearchContext = vi.fn()

    render(
      <MemoryRouter>
        <ProjectWorkspaceContent
          branch="main"
          changedFiles={0}
          cwd="/tmp/kiwi"
          notionConnected
          onCreateProject={vi.fn()}
          onOpenSession={vi.fn()}
          onSearchContext={onSearchContext}
          onSelectProject={vi.fn()}
          onStartTask={onStartTask}
          project={project}
          projects={[project]}
          sessions={[]}
          slackChannelIds={['C123ABC']}
        />
      </MemoryRouter>
    )

    const input = screen.getByRole('textbox', { name: 'Task request' })
    fireEvent.change(input, { target: { value: 'Notion 결정사항을 확인하고 랜딩페이지를 수정해줘' } })

    act(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Start task' }))
    })

    expect(onStartTask).toHaveBeenCalledWith('Notion 결정사항을 확인하고 랜딩페이지를 수정해줘')

    fireEvent.change(screen.getByRole('textbox', { name: 'Project context query' }), {
      target: { value: '온보딩 결정사항' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Search project context' }))

    expect(onSearchContext).toHaveBeenCalledWith('온보딩 결정사항', ['notion', 'slack'])
    expect(onStartTask).toHaveBeenCalledTimes(1)
  })

  it('keeps selected file and image artifacts in the task draft until removed', () => {
    const onPickAttachments = vi.fn()
    const onRemoveAttachment = vi.fn()

    render(
      <MemoryRouter>
        <ProjectWorkspaceContent
          attachments={[
            {
              id: 'image:reference',
              kind: 'image',
              label: 'reference.png',
              previewUrl: 'data:image/png;base64,AAAA'
            }
          ]}
          branch="main"
          changedFiles={0}
          cwd={project.path}
          notionConnected
          onCreateProject={vi.fn()}
          onOpenSession={vi.fn()}
          onPickAttachments={onPickAttachments}
          onRemoveAttachment={onRemoveAttachment}
          onSelectProject={vi.fn()}
          onStartTask={vi.fn()}
          project={project}
          projects={[project]}
          sessions={[]}
        />
      </MemoryRouter>
    )

    expect(screen.getByText('reference.png')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Attach files' }))
    fireEvent.click(screen.getByRole('button', { name: 'Attach images' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove reference.png' }))

    expect(onPickAttachments).toHaveBeenNthCalledWith(1, 'file')
    expect(onPickAttachments).toHaveBeenNthCalledWith(2, 'image')
    expect(onRemoveAttachment).toHaveBeenCalledWith('image:reference')
  })

  it('shows every project conversation instead of truncating the activity list', () => {
    const manySessions: WorkspaceSession[] = Array.from({ length: 10 }, (_, index) => ({
      id: `session-${index + 1}`,
      title: `Project conversation ${index + 1}`,
      preview: `Conversation ${index + 1}`,
      active: false,
      busy: false,
      source: 'cli'
    }))

    render(
      <MemoryRouter>
        <ProjectWorkspaceContent
          branch="main"
          changedFiles={0}
          cwd={project.path}
          notionConnected
          onCreateProject={vi.fn()}
          onOpenSession={vi.fn()}
          onSelectProject={vi.fn()}
          onStartTask={vi.fn()}
          project={project}
          projects={[project]}
          sessions={manySessions}
        />
      </MemoryRouter>
    )

    expect(screen.getByText('Project conversation 10')).toBeTruthy()
  })

  it('renders honest loading and retry states for project hydration', () => {
    const { rerender } = render(
      <MemoryRouter>
        <ProjectWorkspaceContent
          branch="main"
          changedFiles={0}
          cwd={project.path}
          notionConnected
          onCreateProject={vi.fn()}
          onOpenSession={vi.fn()}
          onSelectProject={vi.fn()}
          onStartTask={vi.fn()}
          project={project}
          projects={[project]}
          sessions={[]}
          sessionsStatus="loading"
        />
      </MemoryRouter>
    )

    expect(screen.getByRole('status', { name: 'Loading project conversations' })).toBeTruthy()

    const onRetrySessions = vi.fn()
    rerender(
      <MemoryRouter>
        <ProjectWorkspaceContent
          branch="main"
          changedFiles={0}
          cwd={project.path}
          notionConnected
          onCreateProject={vi.fn()}
          onOpenSession={vi.fn()}
          onRetrySessions={onRetrySessions}
          onSelectProject={vi.fn()}
          onStartTask={vi.fn()}
          project={project}
          projects={[project]}
          sessions={[]}
          sessionsStatus="error"
        />
      </MemoryRouter>
    )

    expect(screen.getByText('Project conversations unavailable')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetrySessions).toHaveBeenCalledTimes(1)
  })

  it('keeps path-less projects read-only', () => {
    render(
      <MemoryRouter>
        <ProjectWorkspaceContent
          branch=""
          changedFiles={0}
          cwd=""
          notionConnected
          onCreateProject={vi.fn()}
          onOpenSession={vi.fn()}
          onSelectProject={vi.fn()}
          onStartTask={vi.fn()}
          project={{ ...project, path: '' }}
          projects={[{ ...project, path: '' }]}
          sessions={[]}
        />
      </MemoryRouter>
    )

    const input = screen.getByRole('textbox', { name: 'Task request' })
    fireEvent.change(input, { target: { value: 'Do not run' } })
    expect((screen.getByRole('button', { name: 'Start task' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not offer a runnable task when no project context is selected', () => {
    const onCreateProject = vi.fn()

    render(
      <MemoryRouter>
        <ProjectWorkspaceContent
          branch=""
          changedFiles={0}
          cwd=""
          notionConnected
          onCreateProject={onCreateProject}
          onOpenSession={vi.fn()}
          onSelectProject={vi.fn()}
          onStartTask={vi.fn()}
          project={null}
          projects={[]}
          sessions={[]}
        />
      </MemoryRouter>
    )

    const input = screen.getByRole('textbox', { name: 'Task request' })
    fireEvent.change(input, { target: { value: 'Start a task without a workspace' } })

    expect((screen.getByRole('button', { name: 'Start task' }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Create project' }))
    expect(onCreateProject).toHaveBeenCalledTimes(1)
  })
})
