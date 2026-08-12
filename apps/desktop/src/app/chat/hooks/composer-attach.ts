/**
 * Attach a local file to the MAIN chat composer from outside React — the
 * store-level twin of use-composer-actions' attachImagePath, callable from
 * plugin pages and other non-hook surfaces (SDK `host.attachFileToComposer`).
 *
 * Two delivery paths, because the main composer is NOT globally mounted (it
 * lives inside the chat route — a full-page surface like Media Studio has no
 * live composer):
 *
 *  - composer mounted → add straight to the live scope's atom; the chip
 *    appears immediately.
 *  - composer unmounted → stash into the per-session draft keyed on the main
 *    composer's LAST scope (sticky via markMainComposerDraftScope). The swap
 *    effect restores it, chip included, when the user lands back on chat.
 *
 * Images attach as `kind: 'image'` (inline preview resolved best-effort);
 * everything else attaches as a `kind: 'file'` reference. The user still
 * reviews and sends — this only stages.
 */

import { attachmentId, pathLabel } from '@/lib/chat-runtime'
import {
  addComposerAttachment,
  type ComposerAttachment,
  getMainComposerDraftScope,
  stashSessionDraft,
  takeSessionDraft,
  updateComposerAttachment,
  upsertAttachment
} from '@/store/composer'

import { isComposerMounted } from './composer-attach-presence'
import { attachmentPreviewDataUrl } from './use-composer-actions'

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'tiff'])

function isImagePath(filePath: string): boolean {
  const ext = filePath.split('.').pop()?.toLowerCase() ?? ''

  return IMAGE_EXTENSIONS.has(ext)
}

/** Stage into the unmounted composer's per-session draft stash. */
function stashAttachment(attachment: ComposerAttachment): void {
  const scope = getMainComposerDraftScope()
  const draft = takeSessionDraft(scope)

  stashSessionDraft(scope, draft.text, upsertAttachment(draft.attachments, attachment))
}

function updateStashedAttachment(attachment: ComposerAttachment): void {
  const scope = getMainComposerDraftScope()
  const draft = takeSessionDraft(scope)

  if (draft.attachments.some(item => item.id === attachment.id)) {
    stashSessionDraft(scope, draft.text, upsertAttachment(draft.attachments, attachment))
  }
}

export async function attachComposerFile(filePath: string): Promise<boolean> {
  if (!filePath) {
    return false
  }

  const image = isImagePath(filePath)

  const attachment: ComposerAttachment = {
    id: attachmentId(image ? 'image' : 'file', filePath),
    kind: image ? 'image' : 'file',
    label: pathLabel(filePath),
    detail: filePath,
    path: filePath
  }

  const live = isComposerMounted('main')

  if (live) {
    addComposerAttachment(attachment)
  } else {
    stashAttachment(attachment)
  }

  if (image) {
    try {
      const previewUrl = await attachmentPreviewDataUrl(filePath)

      if (previewUrl) {
        const withPreview = { ...attachment, previewUrl }

        // The composer may have (un)mounted while the preview resolved, which
        // moves the attachment between the live atom and the stash (mount
        // restores stash → atom; unmount stashes atom → stash). Update both
        // idempotently — each is a no-op when it doesn't hold the id.
        updateComposerAttachment(withPreview)
        updateStashedAttachment(withPreview)
      }
    } catch {
      // Preview is cosmetic — the attachment itself already landed.
    }
  }

  return true
}
