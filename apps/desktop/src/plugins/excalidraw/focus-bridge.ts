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
  if (!layout) {
    return []
  }

  const profileKey = normalizeProfileKey(profile)

  return documents.flatMap(({ identity, status }) => {
    const group = findGroupOfPane(layout, excalidrawPaneId(identity))

    return status === 'connected' &&
      normalizeProfileKey(identity.profile) === profileKey &&
      group?.active === excalidrawPaneId(identity)
      ? [identity.path]
      : []
  })
}

interface FocusSnapshot {
  profile: string
  sessionId: string
}

const sameFocusSession = (left: FocusSnapshot, right: FocusSnapshot) =>
  left.profile === right.profile && left.sessionId === right.sessionId

export function installFocusedDrawingBridge(): () => void {
  let disposed = false
  let generation = 0
  let lastSession: FocusSnapshot | null = null
  let pending = Promise.resolve()

  const publish = (session: FocusSnapshot, paths: string[]) =>
    host
      .request('excalidraw.focus', {
        paths,
        profile: session.profile,
        session_id: session.sessionId
      })
      .catch(() => undefined)

  const sync = () => {
    if (disposed) {
      return
    }

    const requestGeneration = ++generation
    const sessionId = host.state.activeSessionId.get()
    const profile = host.state.profile.get()
    const session = sessionId ? { profile, sessionId } : null
    const paths = session ? focusedDrawingPaths($excalidrawDocuments.get(), profile, $layoutTree.get()) : []
    const previousSession = lastSession

    pending = pending.then(async () => {
      if (disposed) {
        return
      }

      if (previousSession && (!session || !sameFocusSession(previousSession, session))) {
        await publish(previousSession, [])
      }

      if (disposed || requestGeneration !== generation) {
        return
      }
      if (!session) {
        lastSession = null
        return
      }

      lastSession = session
      void publish(session, paths)
    })
  }

  const off = [
    $excalidrawDocuments.listen(sync),
    $layoutTree.listen(sync),
    host.state.activeSessionId.listen(sync),
    host.state.gateway.listen(sync),
    host.state.profile.listen(sync)
  ]

  sync()

  return () => {
    if (disposed) {
      return
    }
    disposed = true
    generation += 1
    off.forEach(stop => stop())

    const previousSession = lastSession
    lastSession = null
    if (previousSession) {
      void publish(previousSession, [])
    }
  }
}
