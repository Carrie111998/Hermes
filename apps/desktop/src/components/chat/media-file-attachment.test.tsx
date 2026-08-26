import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MediaFileAttachment } from './media-file-attachment'

const isDesktopFsRemoteMode = vi.fn(() => false)

vi.mock('@/lib/desktop-fs', () => ({
  isDesktopFsRemoteMode: () => isDesktopFsRemoteMode()
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  isDesktopFsRemoteMode.mockReturnValue(false)
})

// A delivered file the chat can't render inline used to be a bare `Open <name>`
// anchor. It named the file without saying where the file was, so reaching it
// on disk meant copying the basename out of the sentence and searching for it.
describe('a delivered file says where it is', () => {
  it('shows the containing path under the name', () => {
    render(<MediaFileAttachment failed onOpen={vi.fn()} path="/Users/someone/Downloads/report.csv" />)

    expect(screen.getByText('report.csv')).toBeTruthy()
    expect(screen.getByText('~/Downloads/report.csv')).toBeTruthy()
  })

  // The tildified path is for reading; the absolute one is what you paste.
  it('keeps the absolute path available on hover', () => {
    render(<MediaFileAttachment failed onOpen={vi.fn()} path="/Users/someone/Downloads/report.csv" />)

    expect(screen.getByText('~/Downloads/report.csv').getAttribute('title')).toBe(
      '/Users/someone/Downloads/report.csv'
    )
  })

  it('resolves a file: URL delivery to its real path', () => {
    render(<MediaFileAttachment failed onOpen={vi.fn()} path="file:///Users/someone/Downloads/report.csv" />)

    expect(screen.getByText('~/Downloads/report.csv')).toBeTruthy()
  })

  it('still opens the file, which was the only action before', () => {
    const onOpen = vi.fn()

    render(<MediaFileAttachment failed onOpen={onOpen} path="/Users/someone/Downloads/report.csv" />)

    fireEvent.click(screen.getByRole('button', { name: 'Open' }))

    expect(onOpen).toHaveBeenCalled()
  })
})

// Printing a path the user cannot navigate to is worse than printing none: a
// gateway path names another machine's disk, and a URL names no disk at all.
describe('the path is shown only when the file is on this disk', () => {
  it('omits it in remote-gateway mode', () => {
    isDesktopFsRemoteMode.mockReturnValue(true)

    render(<MediaFileAttachment failed onOpen={vi.fn()} path="/srv/gateway/out/report.csv" />)

    expect(screen.getByText('report.csv')).toBeTruthy()
    expect(screen.queryByText('/srv/gateway/out/report.csv')).toBeNull()
  })

  it('omits it for an http delivery', () => {
    render(<MediaFileAttachment failed onOpen={vi.fn()} path="https://example.com/report.csv" />)

    expect(screen.getByText('report.csv')).toBeTruthy()
    expect(screen.queryByText(/example\.com/)).toBeNull()
  })
})
