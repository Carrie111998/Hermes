import { beforeEach, describe, expect, it, vi } from 'vitest'

import { normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { openPreview } from '@/store/preview'

import type { ArtifactRecord } from './artifact-utils'
import { openArtifactRecordInPreview } from './open-artifact'

vi.mock('@/lib/local-preview', () => ({
  normalizeOrLocalPreviewTarget: vi.fn()
}))

vi.mock('@/store/preview', () => ({
  openPreview: vi.fn()
}))

const record: ArtifactRecord = {
  href: 'file:///work/output/report.html',
  id: 'session:report',
  kind: 'file',
  label: 'report.html',
  sessionId: 'session',
  sessionTitle: 'Build report',
  timestamp: 1,
  value: '/work/output/report.html'
}

describe('artifact library preview routing', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('uses the file-tree preview resolver and opens a nested Preview tab', async () => {
    vi.mocked(normalizeOrLocalPreviewTarget).mockResolvedValue({
      kind: 'file',
      label: 'report.html',
      path: record.value,
      previewKind: 'html',
      source: record.value,
      url: record.href
    })

    await expect(openArtifactRecordInPreview(record, '/work')).resolves.toBe(true)
    expect(normalizeOrLocalPreviewTarget).toHaveBeenCalledWith(record.value, '/work')
    expect(openPreview).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'file', label: record.label, url: record.href }),
      'manual'
    )
  })

  it('does not create an empty tab when the target cannot be resolved', async () => {
    vi.mocked(normalizeOrLocalPreviewTarget).mockResolvedValue(null)

    await expect(openArtifactRecordInPreview(record)).resolves.toBe(false)
    expect(openPreview).not.toHaveBeenCalled()
  })
})
