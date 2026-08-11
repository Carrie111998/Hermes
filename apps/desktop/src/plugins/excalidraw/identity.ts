export interface ExcalidrawDocumentIdentity {
  path: string
  profile: string
  runtime: string
}

export function excalidrawDocumentKey({ profile, runtime, path }: ExcalidrawDocumentIdentity): string {
  return `${profile}\u0000${runtime}\u0000${path}`
}

export function excalidrawPaneId(identity: ExcalidrawDocumentIdentity): string {
  return `excalidraw:${encodeURIComponent(excalidrawDocumentKey(identity))}`
}
