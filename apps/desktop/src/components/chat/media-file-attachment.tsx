import type { ReactNode } from 'react'

import { useI18n } from '@/i18n'
import { isDesktopFsRemoteMode } from '@/lib/desktop-fs'
import { displayPath } from '@/lib/display-path'
import { FileText } from '@/lib/icons'
import { filePathFromMediaPath, isFileMediaPath, mediaName } from '@/lib/media'

interface MediaFileAttachmentProps {
  /** Rendered under the card (the gateway-fetch failure note). */
  children?: ReactNode
  /** Whether the delivered file failed to load as playable/inline media. */
  failed: boolean
  /** Fires the existing open/download behaviour for this attachment. */
  onOpen: () => void
  /** The delivered MEDIA path, as written by the agent. */
  path: string
}

/**
 * A delivered file the chat cannot render inline (a `.csv`, a `.zip`).
 *
 * The old shape was a bare `Open <name>` anchor, which told you a file existed
 * without telling you where: the label carried the basename only, so locating
 * the file on disk meant copying that name out of the sentence and searching
 * for it. This card keeps the same single action and adds the answer to "where
 * is it" — the home-relative path under the name, with the absolute path on
 * hover.
 *
 * Shape follows `PreviewAttachment`, the sibling card for delivered files the
 * rail CAN render, so the two deliveries read as the same kind of object.
 */
export function MediaFileAttachment({ children, failed, onOpen, path }: MediaFileAttachmentProps) {
  const { t } = useI18n()
  const name = mediaName(path)
  const filePath = filePathFromMediaPath(path)

  // Only a path on THIS disk is worth printing. A remote gateway's paths point
  // at the gateway machine and a URL delivery has no local file, so showing
  // either would name a location the user cannot navigate to.
  const local = !isDesktopFsRemoteMode() && isFileMediaPath(path)
  const shown = displayPath(filePath)

  return (
    <span className="block">
      <span className="my-2 flex w-full max-w-160 items-center gap-2 rounded-lg border border-(--ui-stroke-tertiary) bg-card/55 px-2.5 py-1.5 text-sm">
        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-muted/55 text-muted-foreground/85">
          <FileText className="size-3.5" />
        </span>
        <span className="flex min-w-0 flex-1 flex-col">
          <span className="truncate text-[0.78rem] font-medium text-foreground/90">{name}</span>
          {local && shown !== name ? (
            <span className="truncate text-[0.68rem] text-muted-foreground" title={filePath}>
              {shown}
            </span>
          ) : null}
        </span>
        <button
          className="shrink-0 rounded-md border border-(--ui-stroke-tertiary) bg-background/40 px-2 py-1 text-[0.7rem] font-medium text-muted-foreground transition-colors hover:bg-accent/55 hover:text-foreground"
          onClick={onOpen}
          type="button"
        >
          {failed ? t.fileMenu.open : t.fileMenu.opening}
        </button>
      </span>
      {children}
    </span>
  )
}
