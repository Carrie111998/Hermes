// Shared mutable core of the pen host: the document registry, the lazy
// runtime handle, the renderer-facing event feed, and the logger. Every
// sibling module leans on this one; it imports none of them.

import { EventEmitter } from 'node:events'

import type { PenInstallation } from '../pen-host'

// ---------------------------------------------------------------------------
// Logging — quiet by default; pen host chatter is debug-only noise.
// ---------------------------------------------------------------------------

const log = {
  debug: (..._args: unknown[]) => {},
  info: (...args: unknown[]) => console.log('[pen]', ...args),
  warn: (...args: unknown[]) => console.warn('[pen]', ...args),
  error: (...args: unknown[]) => console.error('[pen]', ...args)
}
// ---------------------------------------------------------------------------
// Document registry — one entry per open canvas document.
// ---------------------------------------------------------------------------

export interface PenDocumentInfo {
  docId: string
  fileURI: string
  displayName: string
  isTemporary: boolean
}

export interface PenDocument {
  docId: string
  fileURI: string
  device: any // HermesPenResourceDevice — null for web-editor documents
  ipc: any | null // @ha/shared IPCHost bound to the webview guest
  guestWebContentsId: number | null
  /** True when this document is hosted by the pen.dev WEB editor (app.pen.dev)
   *  rather than the installed Pen.app bundle. Web documents have no @ha/*
   *  device or IPC host — persistence + tools live in the page (IndexedDB +
   *  WebMCP). Every device-touching path must guard on this. */
  web?: boolean
  /** Display name for web documents (bundle docs derive it from fileURI). */
  displayName?: string
}

export interface PenRuntime {
  install: PenInstallation
  shared: any // @ha/shared (IPCHost, getDocumentDisplayName, URI helpers)
  ipcLib: any // @ha/ipc (TransportServerManager, IPCDeviceManager, …)
  mcpLib: any // @ha/mcp (getMcpConfiguration)
  transportServer: any
  deviceManager: any
}

/** The live pen runtime. Mutable module state read everywhere; WRITTEN only
 *  through setPenRuntime so cross-module ESM live bindings stay correct. */
export let runtime: PenRuntime | null = null

export function setPenRuntime(next: PenRuntime | null): void {
  runtime = next
}
export const documents = new Map<string, PenDocument>()
export const events = new EventEmitter()

/** Renderer-facing change feed (documents opened/closed, agents connected). */
export function onPenEvent(event: string, listener: (...args: any[]) => void): () => void {
  events.on(event, listener)

  return () => events.off(event, listener)
}

export { log }
