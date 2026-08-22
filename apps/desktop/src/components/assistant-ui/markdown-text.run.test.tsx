import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $chatTerminalRunRequest, $terminalTakeover, takeChatTerminalRunRequest } from '@/app/right-sidebar/store'
import { $activeTerminalId, $terminals } from '@/app/right-sidebar/terminal/terminals'
import { isRunnableShellLanguage } from '@/components/chat/shiki-highlighter'
import { $currentCwd } from '@/store/session'

import { MarkdownTextContent } from './markdown-text'

function fenced(language: string, body: string): string {
  return `\`\`\`${language}\n${body}\n\`\`\``
}

function installTerminalBridge() {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: { terminal: { start: vi.fn(), write: vi.fn() } }
  })
}

describe('assistant markdown Run affordance security boundary', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $terminals.set([])
    $activeTerminalId.set(null)
    $chatTerminalRunRequest.set(null)
    $terminalTakeover.set(false)
    $currentCwd.set('/workspace')
    installTerminalBridge()
  })

  afterEach(() => {
    cleanup()
    $chatTerminalRunRequest.set(null)
    $terminals.set([])
    $activeTerminalId.set(null)
    $terminalTakeover.set(false)
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    window.localStorage.clear()
  })

  it('keeps shared/reasoning markdown copy-only unless a trusted caller opts in', () => {
    render(<MarkdownTextContent isRunning={false} text={fenced('bash', 'echo hello')} />)
    expect(screen.queryByRole('button', { name: 'Run' })).toBeNull()
  })

  it('queues an opted-in completed shell block into a fresh user terminal', () => {
    render(<MarkdownTextContent allowRunCommands isRunning={false} text={fenced('bash', 'echo hello')} />)

    const run = screen.getByRole('button', { name: 'Run' })
    expect(run.getAttribute('data-slot')).toBe('button')
    expect(run.hasAttribute('title')).toBe(false)
    expect(run.className).toContain('pointer-events-none')
    expect(run.className).toContain('group-hover/code:pointer-events-auto')

    fireEvent.click(run)

    const request = $chatTerminalRunRequest.get()
    expect(request?.command).toBe('echo hello')
    expect(request?.terminalId).toBe($activeTerminalId.get())
    expect($terminals.get().find(term => term.id === request?.terminalId)?.kind).toBe('user')
    expect($terminalTakeover.get()).toBe(true)
  })

  it('coalesces an in-flight repeat click but allows a later deliberate rerun', () => {
    render(<MarkdownTextContent allowRunCommands isRunning={false} text={fenced('bash', 'echo once')} />)
    const run = screen.getByRole('button', { name: 'Run' })

    fireEvent.click(run)
    const first = $chatTerminalRunRequest.get()
    const count = $terminals.get().length
    expect(first).not.toBeNull()

    fireEvent.click(run)
    expect($terminals.get()).toHaveLength(count)
    expect($chatTerminalRunRequest.get()).toEqual(first)

    expect(takeChatTerminalRunRequest(first!.terminalId)).toBe('echo once')
    fireEvent.click(run)

    expect($terminals.get()).toHaveLength(count + 1)
    expect($chatTerminalRunRequest.get()?.command).toBe('echo once')
    expect($chatTerminalRunRequest.get()?.terminalId).not.toBe(first!.terminalId)
  })

  it('hides Run for unsafe invisible payloads, streaming, missing bridge, and output/data fences', () => {
    render(<MarkdownTextContent allowRunCommands isRunning={false} text={fenced('bash', `echo safe\u202E ; id`)} />)
    expect(screen.queryByRole('button', { name: 'Run' })).toBeNull()

    cleanup()
    render(<MarkdownTextContent allowRunCommands isRunning text={fenced('bash', 'echo partial')} />)
    expect(screen.queryByRole('button', { name: 'Run' })).toBeNull()

    cleanup()
    render(<MarkdownTextContent allowRunCommands isRunning={false} text={fenced('console', '$ echo hello\nhello')} />)
    expect(screen.queryByRole('button', { name: 'Run' })).toBeNull()

    cleanup()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
    render(<MarkdownTextContent allowRunCommands isRunning={false} text={fenced('bash', 'echo hello')} />)
    expect(screen.queryByRole('button', { name: 'Run' })).toBeNull()
  })

  it('recognizes only explicit shell fence languages', () => {
    for (const language of [
      'bash',
      'bat',
      'batch',
      'cmd',
      'fish',
      'nu',
      'nushell',
      'powershell',
      'ps1',
      'pwsh',
      'sh',
      'shell',
      'shellscript',
      'zsh'
    ]) {
      expect(isRunnableShellLanguage(language)).toBe(true)
    }

    for (const language of [undefined, 'text', 'console', 'python', 'json', 'typescript']) {
      expect(isRunnableShellLanguage(language)).toBe(false)
    }
  })
})
