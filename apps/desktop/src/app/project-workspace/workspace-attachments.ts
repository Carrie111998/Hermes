import { formatRefValue } from '@/components/assistant-ui/directive-text'
import { attachmentId, contextPath, pathLabel } from '@/lib/chat-runtime'
import { readDesktopFileDataUrl, selectDesktopPaths } from '@/lib/desktop-fs'
import type { ComposerAttachment } from '@/store/composer'

export type WorkspaceAttachmentKind = 'file' | 'image'

export interface PickWorkspaceAttachmentsOptions {
  cwd: string
  kind: WorkspaceAttachmentKind
}

const IMAGE_FILTERS = [
  {
    extensions: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tiff'],
    name: 'Images'
  }
]

export async function pickWorkspaceAttachments({
  cwd,
  kind
}: PickWorkspaceAttachmentsOptions): Promise<ComposerAttachment[]> {
  const paths = await selectDesktopPaths({
    defaultPath: cwd || undefined,
    filters: kind === 'image' ? IMAGE_FILTERS : undefined,
    title: kind === 'image' ? 'Add images as context' : 'Add files as context'
  })

  const attachments: ComposerAttachment[] = []

  for (const path of paths) {
    if (kind === 'image') {
      attachments.push({
        detail: path,
        id: attachmentId('image', path),
        kind: 'image',
        label: pathLabel(path),
        path,
        previewUrl: await readDesktopFileDataUrl(path)
      })

      continue
    }

    const relativePath = contextPath(path, cwd)

    attachments.push({
      detail: relativePath,
      id: attachmentId('file', relativePath),
      kind: 'file',
      label: pathLabel(path),
      path,
      refText: `@file:${formatRefValue(relativePath)}`
    })
  }

  return [...new Map(attachments.map(attachment => [attachment.id, attachment])).values()]
}
