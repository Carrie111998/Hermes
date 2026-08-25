import { describe, expect, it } from 'vitest'
import { downloadTextFile } from '@/lib/download-text'

describe('downloadTextFile', () => {
  it('creates a download without errors', () => {
    // Mock DOM APIs
    const mockLink = { href: '', download: '', rel: '', click: () => {}, remove: () => {} }
    const origCreate = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockReturnValue(mockLink as unknown as HTMLElement)
    vi.spyOn(document.body, 'appendChild').mockImplementation(() => mockLink as unknown as Node)
    vi.spyOn(mockLink, 'remove').mockImplementation(() => {})

    expect(() => downloadTextFile('test.txt', 'hello world')).not.toThrow()

    vi.restoreAllMocks()
  })
})
