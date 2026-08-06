/**
 * Shiki Worker - Off-main-thread tokenization for syntax highlighting
 * 
 * This worker handles Shiki's codeToTokens calls to prevent main thread blocking
 * when rendering large diffs or code blocks.
 */

import type { BundledLanguage, ThemedToken } from 'shiki'

// Message types for worker communication
export interface WorkerMessage<T = unknown> {
  type: 'tokenize' | 'tokenizeChunk' | 'cancel' | 'init'
  id: number
  payload?: T
}

export interface TokenizePayload {
  code: string
  language: BundledLanguage
  theme: 'github-dark-dimmed' | 'github-light-default'
}

export interface TokenizeChunkPayload {
  code: string
  language: BundledLanguage
  theme: 'github-dark-dimmed' | 'github-light-default'
  chunkIndex: number
}

export interface WorkerResponse<T = unknown> {
  type: 'result' | 'error' | 'progress' | 'init'
  id: number
  payload?: T
  error?: string
  success?: boolean
}

export interface TokenizeResult {
  tokens: ThemedToken[][]
}

export interface TokenizeChunkResult {
  tokens: ThemedToken[][]
  chunkIndex: number
}

// Global state
let shikiLoaded = false
let codeToTokens: (code: string, options: { lang: BundledLanguage; theme: string }) => Promise<{ tokens: ThemedToken[][] }>
const pendingRequests = new Map<number, (response: WorkerResponse) => void>()

// Initialize Shiki in the worker
async function initShiki(messageId: number) {
  if (shikiLoaded) {return}
  
  try {
    const shiki = await import('shiki')
    codeToTokens = shiki.codeToTokens
    shikiLoaded = true
    self.postMessage({ type: 'init', id: messageId, success: true } satisfies WorkerResponse)
  } catch (error) {
    self.postMessage({ 
      type: 'init', 
      id: messageId,
      success: false, 
      error: error instanceof Error ? error.message : 'Failed to load Shiki' 
    } satisfies WorkerResponse)
  }
}

// Handle tokenization request
async function handleTokenize(message: WorkerMessage<TokenizePayload>) {
  if (!shikiLoaded) {
    await initShiki(message.id)
  }
  
  if (!codeToTokens) {
    pendingRequests.get(message.id)?.({
      type: 'error',
      id: message.id,
      error: 'Shiki not initialized'
    } satisfies WorkerResponse)

    return
  }
  
  try {
    const { code, language, theme } = message.payload!
    const result = await codeToTokens(code, { lang: language, theme })
    
    pendingRequests.get(message.id)?.({
      type: 'result',
      id: message.id,
      payload: { tokens: result.tokens } satisfies TokenizeResult
    } satisfies WorkerResponse)
  } catch (error) {
    pendingRequests.get(message.id)?.({
      type: 'error',
      id: message.id,
      error: error instanceof Error ? error.message : 'Tokenization failed'
    } satisfies WorkerResponse)
  }
}

// Handle chunk tokenization request
async function handleTokenizeChunk(message: WorkerMessage<TokenizeChunkPayload>) {
  if (!shikiLoaded) {
    await initShiki(message.id)
  }
  
  if (!codeToTokens) {
    pendingRequests.get(message.id)?.({
      type: 'error',
      id: message.id,
      error: 'Shiki not initialized'
    } satisfies WorkerResponse)

    return
  }
  
  try {
    const { code, language, theme, chunkIndex } = message.payload!
    const result = await codeToTokens(code, { lang: language, theme })
    
    pendingRequests.get(message.id)?.({
      type: 'result',
      id: message.id,
      payload: { 
        tokens: result.tokens,
        chunkIndex 
      } satisfies TokenizeChunkResult
    } satisfies WorkerResponse)
  } catch (error) {
    pendingRequests.get(message.id)?.({
      type: 'error',
      id: message.id,
      error: error instanceof Error ? error.message : 'Chunk tokenization failed'
    } satisfies WorkerResponse)
  }
}

// Message handler
self.onmessage = async (event: MessageEvent<WorkerMessage>) => {
  const message = event.data
  
  switch (message.type) {
    case 'init':
      await initShiki(message.id)

      break
      
    case 'tokenize':
      pendingRequests.set(message.id, (response) => {
        self.postMessage(response)
      })
      await handleTokenize(message as WorkerMessage<TokenizePayload>)

      break
      
    case 'tokenizeChunk':
      pendingRequests.set(message.id, (response) => {
        self.postMessage(response)
      })
      await handleTokenizeChunk(message as WorkerMessage<TokenizeChunkPayload>)

      break
      
    case 'cancel':
      // For future: implement cancellation token support
      break
  }
}

// Export types for TypeScript consumers
export type { ThemedToken }