import { localPreviewTarget } from '@/lib/local-preview'
import { EXCALIDRAW_CHAT_LINK_PREFIX } from '@/lib/markdown-preprocess'

import type { ExcalidrawDocumentIdentity } from './identity'

export interface ExcalidrawChatScope {
  cwd: string
  profile: string
  runtime: string
}

export function excalidrawIdentityFromChatHref(
  href: string | undefined,
  scope: ExcalidrawChatScope
): ExcalidrawDocumentIdentity | null {
  if (!href || !scope.cwd || !scope.profile || !scope.runtime) {
    return null
  }

  const targetHref = href.startsWith(EXCALIDRAW_CHAT_LINK_PREFIX)
    ? href.slice(EXCALIDRAW_CHAT_LINK_PREFIX.length)
    : href

  const target = localPreviewTarget(targetHref, scope.cwd)

  if (target?.kind !== 'file' || !target.path) {
    return null
  }

  const path = target.path.split(/[?#]/, 1)[0]

  return path.toLowerCase().endsWith('.excalidraw') ? { path, profile: scope.profile, runtime: scope.runtime } : null
}
