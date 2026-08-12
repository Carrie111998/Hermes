import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { previewFile } = vi.hoisted(() => ({ previewFile: vi.fn() }))

vi.mock('@hermes/plugin-sdk', () => ({
  Codicon: ({ name }: { name: string }) => <span>{name}</span>,
  host: { previewFile }
}))

import { AttachmentList } from './attachment-list'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Kanban attachment list', () => {
  it('opens an attachment in the shared preview pane', () => {
    render(
      <AttachmentList
        attachments={[
          {
            id: 8,
            filename: 'evidence.png',
            stored_path: '/tmp/evidence.png'
          }
        ]}
      />
    )

    fireEvent.click(screen.getByRole('button', { name: 'Preview evidence.png' }))

    expect(previewFile).toHaveBeenCalledWith('/tmp/evidence.png', 'evidence.png')
  })

  it('leaves legacy attachments without a stored path as plain text', () => {
    render(<AttachmentList attachments={[{ id: 9, filename: 'legacy.csv' }]} />)

    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByText('legacy.csv')).toBeTruthy()
  })
})
