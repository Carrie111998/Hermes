import { createElement } from 'react'

import { registerPaneCloser, removeTreePane, revealTreePane } from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { Codecs, persistentAtom } from '@/lib/persisted'

import type { DrawingController } from './document'
import { ExcalidrawPane } from './ExcalidrawPane'
import { type ExcalidrawDocumentIdentity, excalidrawDocumentKey, excalidrawPaneId } from './identity'

const PANES_AREA = 'panes'

export type ExcalidrawDocumentStatus = 'connected' | 'disconnected'

export interface ExcalidrawDocument {
  fingerprint: string
  identity: ExcalidrawDocumentIdentity
  status: ExcalidrawDocumentStatus
}

interface RestoredExcalidrawDocuments {
  availableRuntimes: readonly string[]
  documents: readonly ExcalidrawDocument[]
}


const drawingName = (path: string): string => path.split('/').filter(Boolean).at(-1) ?? path
type DrawingControllerHandle = Pick<DrawingController, 'canCloseCleanly' | 'reconcileExternalChange' | 'waitForSave'>

const controllers = new Map<string, DrawingControllerHandle>()
const registered = new Map<string, () => void>()

const sanitizeDocuments = (value: unknown): ExcalidrawDocument[] => {
  if (!Array.isArray(value)) {return []}

  return value.flatMap(document => {
    if (!document || typeof document !== 'object') {return []}
    const candidate = document as Partial<ExcalidrawDocument>
    const identity = candidate.identity

    if (
      !identity ||
      typeof identity !== 'object' ||
      typeof identity.path !== 'string' ||
      typeof identity.profile !== 'string' ||
      typeof identity.runtime !== 'string' ||
      typeof candidate.fingerprint !== 'string'
    ) {
      return []
    }

    return [{ fingerprint: candidate.fingerprint, identity, status: 'disconnected' }]
  })
}

export const $excalidrawDocuments = persistentAtom<ExcalidrawDocument[]>(
  'hermes.desktop.excalidraw.documents',
  [],
  Codecs.json(sanitizeDocuments)
)

function registerDrawingPane(identity: ExcalidrawDocumentIdentity) {
  const key = excalidrawDocumentKey(identity)

  if (registered.has(key)) {return}

  const paneId = excalidrawPaneId(identity)
  registered.set(
    key,
    registry.register({
      area: PANES_AREA,
      data: { placement: 'right' },
      id: paneId,
      render: () => createElement(ExcalidrawPane, { identity }),
      title: drawingName(identity.path)
    })
  )
  registerPaneCloser(paneId, () => {
    if (!controllers.has(key)) {
      closeDrawing(identity)

      return
    }

    void requestDrawingClose(identity, () => window.confirm(`Discard unsaved changes to ${drawingName(identity.path)}?`))
  })
}

export function openDrawing(identity: ExcalidrawDocumentIdentity, fingerprint: string): void {
  registerDrawingPane(identity)
  const key = excalidrawDocumentKey(identity)
  const document = { fingerprint, identity, status: 'connected' } satisfies ExcalidrawDocument
  const documents = $excalidrawDocuments.get()
  const existing = documents.findIndex(item => excalidrawDocumentKey(item.identity) === key)
  $excalidrawDocuments.set(existing < 0 ? [...documents, document] : documents.with(existing, document))
  revealTreePane(excalidrawPaneId(identity))
}

export function setDrawingController(identity: ExcalidrawDocumentIdentity, controller: DrawingControllerHandle | null): void {
  const key = excalidrawDocumentKey(identity)

  if (controller) {controllers.set(key, controller)}
  else {controllers.delete(key)}
}

export async function handleChangedDocument(identity: ExcalidrawDocumentIdentity, fingerprint: string): Promise<void> {
  const key = excalidrawDocumentKey(identity)

  if (!$excalidrawDocuments.get().some(document => excalidrawDocumentKey(document.identity) === key)) {return}
  await controllers.get(key)?.reconcileExternalChange(fingerprint)
}

export async function requestDrawingClose(identity: ExcalidrawDocumentIdentity, confirmDiscard: () => boolean | Promise<boolean> = () => false): Promise<boolean> {
  const controller = controllers.get(excalidrawDocumentKey(identity))
  await controller?.waitForSave()

  if (!controller?.canCloseCleanly() && !(await confirmDiscard())) {return false}
  closeDrawing(identity)

  return true
}

export function closeDrawing(identity: ExcalidrawDocumentIdentity): void {
  const key = excalidrawDocumentKey(identity)
  const paneId = excalidrawPaneId(identity)
  controllers.delete(key)
  registered.get(key)?.()
  registered.delete(key)
  registerPaneCloser(paneId)
  removeTreePane(paneId)
  $excalidrawDocuments.set($excalidrawDocuments.get().filter(document => excalidrawDocumentKey(document.identity) !== key))
}

export function restoreExcalidrawDocuments(availableRuntimes: readonly string[]): void {
  const available = new Set(availableRuntimes)
  const documents = $excalidrawDocuments.get()
  documents.forEach(document => registerDrawingPane(document.identity))
  $excalidrawDocuments.set(
    documents.map(document => ({
      ...document,
      status: available.has(document.identity.runtime) ? 'connected' : 'disconnected'
    }))
  )
}

export function resetExcalidrawDocumentsForTest(restored?: RestoredExcalidrawDocuments): void {
  registered.forEach(dispose => dispose())
  controllers.clear()
  registered.clear()
  $excalidrawDocuments.set(restored ? [...restored.documents] : [])

  if (restored) {restoreExcalidrawDocuments(restored.availableRuntimes)}
}
