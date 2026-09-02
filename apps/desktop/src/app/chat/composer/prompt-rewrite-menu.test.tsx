import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'
import { I18nProvider } from '@/i18n'

import { PromptRewriteMenu } from './prompt-rewrite-menu'

const notifications = vi.hoisted(() => ({ notify: vi.fn(), notifyError: vi.fn() }))

vi.mock('@/store/notifications', () => notifications)

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderMenu({
  cwd = '/repo',
  draft = 'fix the scanner end to end',
  request = vi.fn(async () => ({ text: 'Rewritten prompt' })),
  sessionId = 'bot-runtime-1'
}: {
  cwd?: null | string
  draft?: string
  request?: ReturnType<typeof vi.fn>
  sessionId?: null | string
} = {}) {
  let liveDraft = draft

  const onRewrite = vi.fn((text: string) => {
    liveDraft = text
  })

  const gateway = { request } as unknown as HermesGateway

  render(
    <I18nProvider configClient={null} initialLocale="en">
      <PromptRewriteMenu
        cwd={cwd}
        disabled={false}
        gateway={gateway}
        getDraft={() => liveDraft}
        onRewrite={onRewrite}
        sessionId={sessionId}
      />
    </I18nProvider>
  )

  return { onRewrite, request, setDraft: (value: string) => (liveDraft = value) }
}

async function choose(label: RegExp) {
  const trigger = screen.getByRole('button', { name: 'Rewrite' })

  fireEvent.pointerDown(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.pointerUp(trigger, { button: 0, pointerType: 'mouse' })
  fireEvent.click(trigger)
  fireEvent.click(await screen.findByRole('menuitem', { name: label }))
}

describe('PromptRewriteMenu', () => {
  it('routes the rewrite through the exact gateway and live Bot/profile session', async () => {
    const { onRewrite, request } = renderMenu()

    await choose(/Basic rewrite/)

    await waitFor(() => expect(onRewrite).toHaveBeenCalledWith('Rewritten prompt'))
    expect(request).toHaveBeenCalledWith(
      'llm.oneshot',
      expect.objectContaining({
        input: 'fix the scanner end to end',
        session_id: 'bot-runtime-1',
        task: 'prompt_rewrite',
        timeout: 180
      }),
      195_000
    )
  })

  it('never overwrites typing that changed while the model was rewriting', async () => {
    let finish!: (value: { text: string }) => void
    const request = vi.fn(() => new Promise<{ text: string }>(resolve => (finish = resolve)))
    const { onRewrite, setDraft } = renderMenu({ request })

    await choose(/Expand with details/)
    setDraft('newer words from the user')
    finish({ text: 'stale model rewrite' })

    await waitFor(() => expect(notifications.notify).toHaveBeenCalled())
    expect(onRewrite).not.toHaveBeenCalled()
    expect(notifications.notify).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'info', title: 'Rewrite' })
    )
  })

  it('stays unavailable when there is no profile gateway or draft', () => {
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <PromptRewriteMenu disabled gateway={null} getDraft={() => ''} onRewrite={vi.fn()} sessionId={null} />
      </I18nProvider>
    )

    expect((screen.getByRole('button', { name: 'Rewrite' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('uses the profile-scoped gateway before a new session has its first message', async () => {
    const { onRewrite, request } = renderMenu({ sessionId: null })

    await choose(/Expand briefly/)

    await waitFor(() => expect(onRewrite).toHaveBeenCalledWith('Rewritten prompt'))
    expect(request).toHaveBeenCalledWith(
      'llm.oneshot',
      expect.objectContaining({ session_id: undefined, task: 'prompt_rewrite', timeout: 180 }),
      195_000
    )
  })

  it('adds detected project facts to Enhance when they are available', async () => {
    const longVerifyCommand = `npm run verify -- ${'unsafe-context'.repeat(20)}`

    const request = vi.fn(async (method: string, _params?: Record<string, unknown>) =>
      method === 'project.facts'
        ? {
            facts: {
              contextFiles: ['AGENTS.md'],
              manifests: ['package.json'],
              packageManagers: ['npm'],
              root: '/private/repo',
              verifyCommands: ['npm run test', longVerifyCommand]
            }
          }
        : { text: 'Codebase-aware rewrite' }
    )

    const { onRewrite } = renderMenu({ request })

    await choose(/^Enhance/)

    await waitFor(() => expect(onRewrite).toHaveBeenCalledWith('Codebase-aware rewrite'))
    expect(request).toHaveBeenNthCalledWith(1, 'project.facts', { cwd: '/repo' })
    expect(request).toHaveBeenNthCalledWith(
      2,
      'llm.oneshot',
      expect.objectContaining({
        instructions: expect.stringContaining('Verification commands: npm run test')
      }),
      195_000
    )
    expect(request.mock.calls[1]?.[1]?.instructions).not.toContain('/private/repo')
    expect(request.mock.calls[1]?.[1]?.instructions).not.toContain(longVerifyCommand)
    expect(request.mock.calls[1]?.[1]?.instructions).toContain('…')
  })

  it('removes an accidental outer code fence from the model result', async () => {
    const request = vi.fn(async () => ({ text: '```markdown\nRewritten prompt\n```' }))
    const { onRewrite } = renderMenu({ request })

    await choose(/Basic rewrite/)

    await waitFor(() => expect(onRewrite).toHaveBeenCalledWith('Rewritten prompt'))
  })

  it('enhances a first prompt without a session or selected project', async () => {
    const { onRewrite, request } = renderMenu({ cwd: '', sessionId: null })

    await choose(/^Enhance/)

    await waitFor(() => expect(onRewrite).toHaveBeenCalledWith('Rewritten prompt'))
    expect(request).toHaveBeenCalledTimes(1)
    expect(request).toHaveBeenCalledWith(
      'llm.oneshot',
      expect.objectContaining({
        instructions: expect.stringContaining('Enhance from the draft alone'),
        session_id: undefined,
        task: 'prompt_rewrite',
        timeout: 180
      }),
      195_000
    )
  })

  it('still enhances when optional project detection is unavailable', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'project.facts') {
        throw new Error('Method not found: project.facts')
      }

      return { text: 'Enhanced from the draft' }
    })

    const { onRewrite } = renderMenu({ request })

    await choose(/^Enhance/)

    await waitFor(() => expect(onRewrite).toHaveBeenCalledWith('Enhanced from the draft'))
    expect(request).toHaveBeenNthCalledWith(1, 'project.facts', { cwd: '/repo' })
    expect(request).toHaveBeenNthCalledWith(
      2,
      'llm.oneshot',
      expect.objectContaining({ instructions: expect.stringContaining('Enhance from the draft alone') }),
      195_000
    )
  })
})
