import { normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { openPreview } from '@/store/preview'

import type { ArtifactRecord } from './artifact-utils'

/**
 * Artifact libraries are indexes, not viewers. Resolve their file/link record
 * through the same preview ladder as the file tree, then hand it to Preview's
 * existing multi-tab store. The URL/path-based tab id deduplicates an artifact
 * already opened from Files while still allowing many different artifacts.
 */
export async function openArtifactRecordInPreview(record: ArtifactRecord, cwd?: null | string): Promise<boolean> {
  const target = await normalizeOrLocalPreviewTarget(record.value || record.href, cwd)

  if (!target) {
    return false
  }

  openPreview({ ...target, label: record.label }, 'manual')

  return true
}
