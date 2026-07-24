import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $previewTabs, closeRightRail } from '@/store/preview'

import { MarkdownTextContent } from './markdown-text'

describe('markdown link preview button', () => {
  beforeEach(() => {
    closeRightRail()
    window.localStorage.clear()
  })

  afterEach(() => {
    cleanup()
    closeRightRail()
    window.localStorage.clear()
  })

  it('renders the 🖥 preview button next to a markdown link', async () => {
    render(<MarkdownTextContent isRunning={false} text="See [example](https://example.com) for details." />)

    const link = await screen.findByRole('link', { name: /example/ })
    expect(link.getAttribute('href')).toMatch(/^https:\/\/example\.com/)

    // The button is a SIBLING of the anchor, never nested inside it
    // (interactive control inside a link is invalid HTML).
    const button = screen.getByRole('button', { name: 'Open in preview pane' })
    expect(link.contains(button)).toBe(false)
  })

  it('opens the link URL in the preview pane when the button is clicked', async () => {
    render(<MarkdownTextContent isRunning={false} text="See [example](https://example.com) for details." />)

    const link = await screen.findByRole('link', { name: /example/ })
    const href = link.getAttribute('href') ?? ''
    fireEvent.click(screen.getByRole('button', { name: 'Open in preview pane' }))

    await waitFor(() => {
      expect($previewTabs.get().some(tab => tab.target.kind === 'url' && tab.target.url === href)).toBe(true)
    })
  })

  it('shows no preview button for non-http links', async () => {
    render(<MarkdownTextContent isRunning={false} text="See [local](/docs/readme.md) for details." />)

    await screen.findByRole('link', { name: /local/ })
    expect(screen.queryByRole('button', { name: 'Open in preview pane' })).toBeNull()
  })
})
