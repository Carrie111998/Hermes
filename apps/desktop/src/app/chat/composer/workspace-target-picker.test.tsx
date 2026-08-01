import { useStore } from '@nanostores/react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $projectTree } from '@/store/projects'
import {
  $currentBranch,
  $currentCwd,
  $newChatWorkspaceTarget,
  setCurrentBranch,
  setCurrentCwdTransient,
  setNewChatWorkspaceTarget
} from '@/store/session'

import { WorkspaceTargetPicker } from './workspace-target-picker'

const projects = [
  {
    id: 'p_app',
    label: 'App',
    path: '/repo/app',
    repos: [{ groups: [], id: '/repo/app', label: 'app', path: '/repo/app', sessionCount: 0 }],
    sessionCount: 0
  },
  {
    id: 'p_docs',
    label: 'Docs',
    path: '/repo/docs',
    repos: [{ groups: [], id: '/repo/docs', label: 'docs', path: '/repo/docs', sessionCount: 0 }],
    sessionCount: 0
  }
]

function ReactivePicker(props: Omit<React.ComponentProps<typeof WorkspaceTargetPicker>, 'cwd'>) {
  const cwd = useStore($currentCwd)

  return <WorkspaceTargetPicker cwd={cwd} {...props} />
}

function renderPicker(props: Omit<React.ComponentProps<typeof WorkspaceTargetPicker>, 'cwd'> = {}) {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <ReactivePicker {...props} />
    </I18nProvider>
  )
}

describe('WorkspaceTargetPicker', () => {
  beforeEach(() => {
    $projectTree.set(projects)
    setCurrentCwdTransient('/repo/app')
    setCurrentBranch('main')
    setNewChatWorkspaceTarget(undefined)
  })

  afterEach(() => {
    cleanup()
    $projectTree.set([])
    setCurrentCwdTransient('')
    setCurrentBranch('')
    setNewChatWorkspaceTarget(undefined)
  })

  it('displays the project inherited by a blank new session without making it explicit', () => {
    renderPicker()

    expect(screen.getByRole('button', { name: 'Projects' }).textContent).toContain('App')
    expect($newChatWorkspaceTarget.get()).toBeUndefined()
  })

  it('switches the one-shot target and current cwd to another visible project', () => {
    renderPicker()

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Projects' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Docs' }))

    expect($newChatWorkspaceTarget.get()).toBe('/repo/docs')
    expect($currentCwd.get()).toBe('/repo/docs')
    expect($currentBranch.get()).toBe('')
    expect(screen.getByRole('button', { name: 'Projects' }).textContent).toContain('Docs')
  })

  it('clears the one-shot target and current cwd when Home is selected', () => {
    renderPicker()

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Projects' }), { button: 0, ctrlKey: false })
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Home' }))

    expect($newChatWorkspaceTarget.get()).toBeNull()
    expect($currentCwd.get()).toBe('')
    expect($currentBranch.get()).toBe('')
    expect(screen.getByRole('button', { name: 'Projects' }).textContent).toContain('Home')
  })

  it('does not render once a session exists or the draft already contains messages', () => {
    const { rerender } = renderPicker({ sessionId: 'runtime-1' })

    expect(screen.queryByRole('button', { name: 'Projects' })).toBeNull()

    rerender(
      <I18nProvider configClient={null} initialLocale="en">
        <WorkspaceTargetPicker cwd="/repo/app" hasMessages />
      </I18nProvider>
    )

    expect(screen.queryByRole('button', { name: 'Projects' })).toBeNull()
  })
})
