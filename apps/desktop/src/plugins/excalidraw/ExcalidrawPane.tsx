import type { ExcalidrawElement } from '@excalidraw/excalidraw/element/types'
import type { AppState, BinaryFiles } from '@excalidraw/excalidraw/types'
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'

import { registerWindowCloseBarrier } from '@/lib/window-close-barrier'

import { createDrawingController, type DrawingController, type DrawingState, loadDrawing } from './document'
import type { ExcalidrawDocumentIdentity } from './identity'
import { openDrawing, setDrawingController } from './store'

const LazyEditor = lazy(async () => {
  await import('@excalidraw/excalidraw/index.css')
  const module = await import('@excalidraw/excalidraw')

  return { default: module.Excalidraw }
})

export interface ExcalidrawPaneProps {
  identity: ExcalidrawDocumentIdentity
}

interface EditorInitialData {
  appState: Partial<AppState>
  elements: readonly ExcalidrawElement[]
  files: BinaryFiles
}
function DrawingControls({ controller, state }: { controller: DrawingController; state: DrawingState }) {
  if (state.status === 'conflict') {
    return (
      <div role="alert">
        This drawing changed on disk.
        <button onClick={() => void controller.reload()}>Reload</button>
        <button onClick={() => void controller.keepPaneVersion()}>Keep pane version</button>
      </div>
    )
  }

  if (state.status === 'error') {
    return (
      <div role="alert">
        {state.error?.message}
        <button onClick={() => void controller.retry()}>Retry</button>
        <button
          onClick={() =>
            void controller.saveAs().then(destination => {
              if (destination) {
                openDrawing(destination, '')
              }
            })
          }
        >
          Save As
        </button>
      </div>
    )
  }

  return null
}

export function ExcalidrawPane({ identity }: ExcalidrawPaneProps) {
  const ignoreInitializationEcho = useRef(true)
  const [controller, setController] = useState<DrawingController | null>(null)
  const [initialData, setInitialData] = useState<EditorInitialData | null>(null)
  const [sceneRevision, setSceneRevision] = useState(0)
  const [state, setState] = useState<DrawingState | null>(null)

  const handleSceneChange = useCallback(
    (elements: readonly ExcalidrawElement[], appState: AppState, files: BinaryFiles) => {
      if (!controller) {
        return
      }
      if (ignoreInitializationEcho.current) {
        ignoreInitializationEcho.current = false

        return
      }

      controller.onSceneChange([...elements], appState as unknown as Record<string, unknown>, files)
    },
    [controller]
  )

  useEffect(() => {
    let disposed = false
    let activeController: DrawingController | null = null
    let unregisterCloseBarrier: (() => void) | undefined

    ignoreInitializationEcho.current = true
    void loadDrawing(identity)
      .then(drawing => {
        if (disposed) {
          return
        }
        activeController = createDrawingController(drawing)
        unregisterCloseBarrier = registerWindowCloseBarrier(() => activeController?.waitForSave() ?? true)

        const loadedState = activeController.getState()
        setController(activeController)
        setDrawingController(identity, activeController)
        setInitialData({
          appState: loadedState.appState as Partial<AppState>,
          elements: loadedState.elements as readonly ExcalidrawElement[],
          files: loadedState.files as BinaryFiles
        })
        setState(loadedState)
      })
      .catch(error => {
        if (!disposed) {
          setState({
            appState: {},
            elements: [],
            envelope: {},
            error,
            files: {},
            fingerprint: '',
            identity,
            status: 'error'
          })
        }
      })

    return () => {
      disposed = true
      setDrawingController(identity, null)
      // The close-barrier registry retains this callback until a concurrent
      // flush has settled, so React cannot drop the only live flush handle.
      unregisterCloseBarrier?.()
    }
  }, [identity])

  // eslint-disable-next-line no-restricted-syntax -- resets the editor's initialization echo after an external remount.
  useEffect(() => {
    if (!controller) {
      return
    }

    return controller.subscribe(origin => {
      const nextState = controller.getState()
      if (origin === 'external' || nextState.status === 'error') {
        setState(nextState)
      }

      if (origin === 'external') {
        ignoreInitializationEcho.current = true
        setInitialData({
          appState: nextState.appState as Partial<AppState>,
          elements: nextState.elements as readonly ExcalidrawElement[],
          files: nextState.files as BinaryFiles
        })
        setSceneRevision(revision => revision + 1)
      }
    })
  }, [controller])

  if (!controller || !initialData || !state) {
    return <div>Loading drawing…</div>
  }

  return (
    <div className="flex size-full min-h-0 flex-col">
      <DrawingControls controller={controller} state={state} />
      <Suspense fallback={<div>Loading editor…</div>}>
        <LazyEditor initialData={initialData} key={sceneRevision} onChange={handleSceneChange} />
      </Suspense>
    </div>
  )
}
