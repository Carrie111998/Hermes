import { findGroupOfPane, type LayoutNode } from '@/components/pane-shell/tree/model'
import { $layoutTree } from '@/components/pane-shell/tree/store'
import { host } from '@/sdk/index'
import { normalizeProfileKey } from '@/store/profile'

import { excalidrawPaneId } from './identity'
import { $excalidrawDocuments, type ExcalidrawDocument } from './store'

export function focusedDrawingPaths(
  documents: readonly ExcalidrawDocument[],
  profile: string,
  layout: LayoutNode | null
): string[] {
  if (!layout) {return []}

  const profileKey = normalizeProfileKey(profile)

  return documents.flatMap(({ identity, status }) => {
    const group = findGroupOfPane(layout, excalidrawPaneId(identity))

    return status === 'connected' && normalizeProfileKey(identity.profile) === profileKey && group?.active === excalidrawPaneId(identity)
      ? [identity.path]
      : []
  })
}

export function installFocusedDrawingBridge(): () => void {
  const sync = () => {
    const sessionId = host.state.activeSessionId.get()

    if (!sessionId) {return}

    void host.request('excalidraw.focus', {
      paths: focusedDrawingPaths($excalidrawDocuments.get(), host.state.profile.get(), $layoutTree.get()),
      profile: host.state.profile.get(),
      session_id: sessionId
    }).catch(() => undefined)
  }

  const off = [$excalidrawDocuments.listen(sync), $layoutTree.listen(sync), host.state.activeSessionId.listen(sync), host.state.gateway.listen(sync), host.state.profile.listen(sync)]

  sync()

  return () => off.forEach(stop => stop())
}
