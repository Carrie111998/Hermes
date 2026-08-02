import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { $previewTabs, closeRightRail } from '@/store/preview'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }

describe('file card in assistant text', () => {
  let initialDesktop: Window['hermesDesktop'] | undefined

  beforeEach(() => {
    closeRightRail()
    window.localStorage.clear()
    initialDesktop = desktopWindow.hermesDesktop
    desktopWindow.hermesDesktop = {
      readFileText: vi.fn().mockResolvedValue({ binary: false, byteSize: 12, text: 'hello world' })
    } as unknown as Window['hermesDesktop']
  })

  afterEach(() => {
    cleanup()
    closeRightRail()
    window.localStorage.clear()

    if (initialDesktop) {
      desktopWindow.hermesDesktop = initialDesktop
    } else {
      delete desktopWindow.hermesDesktop
    }
  })

  it('renders a local path in assistant text as a clickable file card', async () => {
    render(<MarkdownTextContent isRunning={false} text="Saved to /work/demo.md" />)

    const card = await screen.findByRole('button', { name: 'demo.md' })
    expect(card).toBeTruthy()
  })

  it('opens the file in the preview pane when the card is clicked', async () => {
    render(<MarkdownTextContent isRunning={false} text="Saved to /work/demo.md" />)

    const card = await screen.findByRole('button', { name: 'demo.md' })
    fireEvent.click(card)

    await waitFor(() => {
      expect(
        $previewTabs.get().some(tab => tab.target.kind === 'file' && tab.target.path === '/work/demo.md')
      ).toBe(true)
    })
  })

  it('keeps inline code untouched (no cardification inside code spans)', async () => {
    render(<MarkdownTextContent isRunning={false} text="Run `node /work/demo.md` to start." />)

    await screen.findByText(/node/)
    expect(screen.queryByRole('button', { name: 'demo.md' })).toBeNull()
  })
})
