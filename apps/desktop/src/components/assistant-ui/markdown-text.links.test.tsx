import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { __resetLinkTitleCache } from '@/lib/external-link'

import { MarkdownTextContent } from './markdown-text'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

afterEach(() => {
  __resetLinkTitleCache()
  cleanup()

  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('MarkdownTextContent links', () => {
  it('preserves an explicit markdown URL label after it normalizes to the target', async () => {
    const label = 'forgejo.example.com/issues/101'
    const target = `https://${label}`
    const fetchLinkTitle = vi.fn().mockResolvedValue('Fetched Forgejo title')

    desktopWindow.hermesDesktop = { fetchLinkTitle } as unknown as Window['hermesDesktop']

    render(<MarkdownTextContent isRunning={false} text={`[${label}](${target})`} />)

    const link = screen.getByTitle(target)
    await waitFor(() => {
      expect(link.textContent).toContain(label)
    })
    expect(fetchLinkTitle).not.toHaveBeenCalled()
  })

  it('does not turn an explicit markdown URL label into an embed', async () => {
    const label = 'www.youtube.com/watch?v=dQw4w9WgXcQ'
    const target = `https://${label}`
    const fetchLinkTitle = vi.fn().mockResolvedValue('Fetched YouTube title')

    desktopWindow.hermesDesktop = { fetchLinkTitle } as unknown as Window['hermesDesktop']

    render(<MarkdownTextContent isRunning={false} text={`[${label}](${target})`} />)

    const link = screen.getByTitle(target)
    await waitFor(() => {
      expect(link.textContent).toContain(label)
    })
    expect(fetchLinkTitle).not.toHaveBeenCalled()
  })

  it('fetches titles for bare and angle-bracket autolinks', async () => {
    const fetchLinkTitle = vi.fn().mockResolvedValue('Fetched title')

    desktopWindow.hermesDesktop = { fetchLinkTitle } as unknown as Window['hermesDesktop']

    render(<MarkdownTextContent isRunning={false} text="https://example.com/bare <https://example.com/angle>" />)

    await waitFor(() => {
      expect(fetchLinkTitle).toHaveBeenCalledTimes(2)
    })
  })
})
