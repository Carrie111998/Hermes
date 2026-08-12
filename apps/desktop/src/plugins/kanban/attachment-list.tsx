import { Codicon, host } from '@hermes/plugin-sdk'

import type { KanbanAttachment } from './types'

export function AttachmentList({ attachments }: { attachments: KanbanAttachment[] }) {
  return (
    <ul className="flex flex-col gap-1">
      {attachments.map(attachment => (
        <li className="flex items-center gap-1.5 text-[0.75rem] text-(--ui-text-tertiary)" key={attachment.id}>
          <Codicon name="file" size="0.75rem" />
          {attachment.stored_path ? (
            <button
              aria-label={`Preview ${attachment.filename}`}
              className="min-w-0 truncate text-left underline-offset-2 hover:text-foreground hover:underline"
              onClick={() => void host.previewFile(attachment.stored_path!, attachment.filename)}
              title={attachment.filename}
              type="button"
            >
              {attachment.filename}
            </button>
          ) : (
            <span className="min-w-0 truncate" title={attachment.filename}>
              {attachment.filename}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}