// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it } from 'vitest'

import { FoundrlyView } from './index'
import { FOUNDRLY_PORTAL_PARTITION } from './portal-chat-pane'

function renderFoundrly(entry = '/foundrly') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <FoundrlyView />
    </MemoryRouter>
  )
}

describe('Foundrly workspace', () => {
  afterEach(() => {
    cleanup()
  })

  it('opens on Admin copilot with the Foundrly portal partition', () => {
    const view = renderFoundrly()

    const host = view.container.querySelector('[data-foundrly-portal]')
    expect(host?.getAttribute('data-foundrly-portal')).toBe(FOUNDRLY_PORTAL_PARTITION)
    expect(host?.getAttribute('data-portal-src')).toBe(
      'https://admin.intelli-verse-x.ai/admin/portal/chat'
    )
    expect(view.container.querySelector('webview')).toBeInstanceOf(HTMLElement)
    expect(view.container.querySelector('webview')?.getAttribute('partition')).toBe(
      FOUNDRLY_PORTAL_PARTITION
    )
  })

  it('keeps Foundrly home markers on the Home tab', () => {
    renderFoundrly()
    fireEvent.click(screen.getAllByRole('button', { name: 'Home' })[0]!)

    expect(screen.getByText('Foundrly home')).toBeTruthy()
    expect(screen.getByText('Overnight visibility')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Open Admin copilot' })).toBeTruthy()
  })

  it('does not import IX Agency native copilot IPC', () => {
    const root = resolve(process.cwd(), 'src/app/foundrly')
    const index = readFileSync(resolve(root, 'index.tsx'), 'utf8')
    const pane = readFileSync(resolve(root, 'portal-chat-pane.tsx'), 'utf8')

    expect(`${index}\n${pane}`).not.toMatch(/hermes:ix-agency|copilot-tab/)
    expect(pane).toContain('persist:foundrly-portal')
    expect(pane).toContain("document.createElement('webview')")
  })
})
