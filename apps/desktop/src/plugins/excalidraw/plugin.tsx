import type { HermesPlugin } from '@/contrib/plugin'
import { desktopFsCacheKey } from '@/lib/desktop-fs'
import { host } from '@/sdk/index'
import { $connection } from '@/store/session'
import type { RpcEvent } from '@/types/hermes'

import { installFocusedDrawingBridge } from './focus-bridge'
import { type ExcalidrawDocumentIdentity } from './identity'
import { handleChangedDocument, openDrawing, restoreExcalidrawDocuments } from './store'

export function parseDocumentEvent(event: RpcEvent): null | { fingerprint: string; identity: ExcalidrawDocumentIdentity } {
  const payload = event.payload as Partial<{ fingerprint: string; path: string; profile: string; runtime: string }> | undefined

  if (
    !payload ||
    typeof payload.fingerprint !== 'string' ||
    typeof payload.path !== 'string' ||
    typeof payload.profile !== 'string' ||
    typeof payload.runtime !== 'string' ||
    (event.profile !== undefined && event.profile !== payload.profile)
  ) {
    return null
  }

  const runtime = event.profile === undefined ? payload.runtime : event.runtime

  if (typeof runtime !== 'string') {
    return null
  }

  return {
    fingerprint: payload.fingerprint,
    identity: { path: payload.path, profile: payload.profile, runtime }
  }
}

const plugin: HermesPlugin = {
  id: 'excalidraw',
  name: 'Excalidraw',
  defaultEnabled: true,
  register(ctx) {
    restoreExcalidrawDocuments([desktopFsCacheKey()])
    ctx.onDispose($connection.listen(() => restoreExcalidrawDocuments([desktopFsCacheKey()])))
    ctx.onDispose(installFocusedDrawingBridge())
    ctx.onDispose(
      host.onEvent('excalidraw.open', event => {
        const drawing = parseDocumentEvent(event)

        if (drawing) {openDrawing(drawing.identity, drawing.fingerprint)}
      })
    )
    ctx.onDispose(
      host.onEvent('excalidraw.changed', event => {
        const drawing = parseDocumentEvent(event)

        if (drawing) {void handleChangedDocument(drawing.identity, drawing.fingerprint)}
      })
    )
  }
}

export default plugin
