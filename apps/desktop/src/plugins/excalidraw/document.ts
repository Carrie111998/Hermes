import {
  isDesktopFsRemoteMode,
  isDesktopFsWriteConflict,
  readDesktopDrawingFileText,
  selectDesktopPaths,
  writeDesktopDrawingFileText
} from '@/lib/desktop-fs'

import type { ExcalidrawDocumentIdentity } from './identity'

export type DrawingStatus = 'loading' | 'ready' | 'saving' | 'saved' | 'error' | 'conflict' | 'disconnected'

export interface LoadedDrawing {
  appState: Record<string, unknown>
  elements: unknown[]
  envelope: Record<string, unknown>
  files: Record<string, unknown>
  fingerprint: string
  identity: ExcalidrawDocumentIdentity
}

export interface DrawingState extends LoadedDrawing {
  error?: Error
  status: DrawingStatus
}

export type DrawingChangeOrigin = 'editor' | 'external'

function parseDrawing(text: string): Omit<LoadedDrawing, 'fingerprint' | 'identity'> {
  let parsed: unknown

  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('Invalid Excalidraw document')
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Invalid Excalidraw document')
  }
  const envelope = parsed as Record<string, unknown>

  if (envelope.type !== 'excalidraw' || envelope.version !== 2 || !Array.isArray(envelope.elements)) {
    throw new Error('Invalid Excalidraw document')
  }

  const appState = envelope.appState === undefined ? {} : envelope.appState
  const files = envelope.files === undefined ? {} : envelope.files

  if (
    !appState ||
    typeof appState !== 'object' ||
    Array.isArray(appState) ||
    !files ||
    typeof files !== 'object' ||
    Array.isArray(files)
  ) {
    throw new Error('Invalid Excalidraw document')
  }
  const editorAppState = { ...(appState as Record<string, unknown>) }
  delete editorAppState.collaborators

  return {
    appState: editorAppState,
    elements: envelope.elements,
    envelope,
    files: files as Record<string, unknown>
  }
}

export async function loadDrawing(identity: ExcalidrawDocumentIdentity): Promise<LoadedDrawing> {
  const result = await readDesktopDrawingFileText(identity)

  return { ...parseDrawing(result.text), fingerprint: result.fingerprint, identity }
}

export class DrawingController {
  private debounce: ReturnType<typeof setTimeout> | undefined
  private inFlight: Promise<boolean> | undefined
  private listeners = new Set<(origin: DrawingChangeOrigin) => void>()
  private externalConflict = false
  private pending = false
  private state: DrawingState

  constructor(drawing: LoadedDrawing) {
    this.state = { ...drawing, status: 'ready' }
  }

  getState(): DrawingState {
    return this.state
  }

  subscribe(listener: (origin: DrawingChangeOrigin) => void): () => void {
    this.listeners.add(listener)

    return () => this.listeners.delete(listener)
  }

  onSceneChange(
    elements: unknown[],
    appState: Record<string, unknown>,
    files: Record<string, unknown> = this.state.files
  ): void {
    this.state = { ...this.state, elements, appState, error: undefined, files, status: 'ready' }
    this.pending = true
    this.scheduleSave()
    this.emit('editor')
  }

  async retry(): Promise<void> {
    this.pending = true
    await this.savePending()
  }

  async reconcileExternalChange(_fingerprint: string): Promise<void> {
    if (!this.pending && !this.inFlight && (this.state.status === 'ready' || this.state.status === 'saved')) {
      await this.reload()

      return
    }

    clearTimeout(this.debounce)
    this.pending = false
    this.externalConflict = true
    this.state = { ...this.state, status: 'conflict' }
    this.emit('external')
  }

  async waitForSave(): Promise<boolean> {
    clearTimeout(this.debounce)
    await this.savePending()

    return this.canCloseCleanly()
  }

  canCloseCleanly(): boolean {
    return !this.pending && !this.inFlight && (this.state.status === 'ready' || this.state.status === 'saved')
  }

  async reload(): Promise<void> {
    const drawing = await loadDrawing(this.state.identity)
    this.pending = false
    this.externalConflict = false
    this.state = { ...drawing, status: 'ready' }
    this.emit('external')
  }

  async keepPaneVersion(): Promise<void> {
    try {
      const current = await readDesktopDrawingFileText(this.state.identity)
      this.state = { ...this.state, fingerprint: current.fingerprint, error: undefined, status: 'saving' }
      this.emit('external')
      await this.writeCurrent()
    } catch (error) {
      if (isDesktopFsWriteConflict(error)) {
        this.externalConflict = true
        this.state = { ...this.state, error: undefined, status: 'conflict' }
      } else {
        this.state = {
          ...this.state,
          error: error instanceof Error ? error : new Error(String(error)),
          status: 'error'
        }
      }
      this.emit('external')
    }
  }

  async saveAs(): Promise<ExcalidrawDocumentIdentity | null> {
    const remote = isDesktopFsRemoteMode()

    const [selection] = await selectDesktopPaths({
      defaultPath: this.state.identity.path,
      directories: remote,
      filters: remote ? undefined : [{ extensions: ['excalidraw'], name: 'Excalidraw drawing' }],
      multiple: false,
      title: 'Save Excalidraw drawing as'
    })

    if (!selection) {
      return null
    }
    const path = remote ? `${selection.replace(/\/$/, '')}/${this.state.identity.path.split('/').at(-1)}` : selection
    const identity = { ...this.state.identity, path }
    await writeDesktopDrawingFileText(identity, this.serialize(), undefined)

    return identity
  }

  async dispose(): Promise<boolean> {
    const clean = await this.waitForSave()
    this.listeners.clear()

    return clean
  }

  private scheduleSave(): void {
    clearTimeout(this.debounce)
    this.debounce = setTimeout(() => void this.savePending(), 500)
  }

  private async savePending(): Promise<void> {
    if (this.inFlight) {
      await this.inFlight

      if (this.pending) {
        await this.savePending()
      }

      return
    }

    if (!this.pending || this.externalConflict) {
      return
    }
    this.pending = false
    const inFlight = this.save()
    this.inFlight = inFlight
    const saved = await inFlight

    if (this.inFlight === inFlight) {
      this.inFlight = undefined
    }

    if (saved && this.pending) {
      await this.savePending()
    }
  }

  private async save(): Promise<boolean> {
    this.state = { ...this.state, error: undefined, status: 'saving' }
    this.emit('editor')

    try {
      await this.writeCurrent()

      return true
    } catch (error) {
      if (isDesktopFsWriteConflict(error)) {
        this.externalConflict = true
        this.state = { ...this.state, error: undefined, status: 'conflict' }
        this.emit('external')

        return false
      }

      this.pending = true
      this.state = { ...this.state, error: error instanceof Error ? error : new Error(String(error)), status: 'error' }
      this.emit('editor')

      return false
    }
  }

  private async writeCurrent(): Promise<void> {
    const content = this.serialize()
    const result = await writeDesktopDrawingFileText(this.state.identity, content, this.state.fingerprint)
    this.externalConflict = false
    this.state = { ...this.state, error: undefined, fingerprint: result.fingerprint, status: 'saved' }
    this.emit('editor')
  }

  private serialize(): string {
    return JSON.stringify({
      ...this.state.envelope,
      appState: this.state.appState,
      elements: this.state.elements,
      files: this.state.files
    })
  }

  private emit(origin: DrawingChangeOrigin): void {
    this.listeners.forEach(listener => listener(origin))
  }
}

export function createDrawingController(drawing: LoadedDrawing): DrawingController {
  return new DrawingController(drawing)
}
