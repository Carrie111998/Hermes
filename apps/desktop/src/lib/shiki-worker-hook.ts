/**
 * Hook for off-main-thread Shiki tokenization via Web Worker
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { BundledLanguage, ThemedToken } from 'shiki'

interface WorkerMessage<T = unknown> {
  type: 'tokenize' | 'tokenizeChunk' | 'cancel' | 'init'
  id: number
  payload?: T
}

interface WorkerResponse<T = unknown> {
  type: 'result' | 'error' | 'progress' | 'init'
  id: number
  payload?: T
  error?: string
  success?: boolean
}

interface TokenizeResult {
  tokens: ThemedToken[][]
}

interface TokenizeChunkResult {
  tokens: ThemedToken[][]
  chunkIndex: number
}

interface UseShikiWorkerOptions {
  language: BundledLanguage
  theme?: 'github-dark-dimmed' | 'github-light-default'
}

interface UseShikiWorkerReturn {
  tokenize: (code: string) => Promise<ThemedToken[][] | null>
  tokenizeChunks: (chunks: Array<{ code: string; chunkIndex: number }>) => Promise<Map<number, ThemedToken[][]>>
  isReady: boolean
  error: string | null
  terminate: () => void
}

const WORKER_URL = new URL('./shiki-worker.ts', import.meta.url).toString()

export function useShikiWorker({ language, theme = 'github-dark-dimmed' }: UseShikiWorkerOptions): UseShikiWorkerReturn {
  const workerRef = useRef<Worker | null>(null)
  const requestIdRef = useRef(0)
  const pendingRef = useRef<Map<number, { resolve: (value: ThemedToken[][] | null) => void; reject: (error: Error) => void }>>(new Map())
  const [isReady, setIsReady] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Initialize worker
  // eslint-disable-next-line no-restricted-syntax -- Worker instance ref is not an atom mirror; it's a DOM/Worker instance stored for cleanup
  useEffect(() => {
    const worker = new Worker(WORKER_URL, { type: 'module' })
    workerRef.current = worker

    worker.onmessage = (event: MessageEvent<WorkerResponse>) => {
      const { type, id, payload, error: workerError, success } = event.data

      if (type === 'init') {
        if (success) {
          setIsReady(true)
          setError(null)
        } else {
          setError(workerError || 'Worker initialization failed')
          setIsReady(false)
        }
        return
      }

      const pending = pendingRef.current.get(id)
      if (!pending) {
        return
      }

      pendingRef.current.delete(id)

      if (type === 'result') {
        // Handle both TokenizeResult and TokenizeChunkResult
        const typedPayload = payload as TokenizeResult | TokenizeChunkResult
        pending.resolve(typedPayload.tokens)
      } else if (type === 'error') {
        pending.reject(new Error(workerError || 'Unknown worker error'))
      }
    }

    worker.onerror = (err) => {
      setError(`Worker error: ${err.message}`)
      setIsReady(false)
    }

    // Initialize worker
    worker.postMessage({ type: 'init', id: ++requestIdRef.current } as WorkerMessage)

    return () => {
      worker.terminate()
      workerRef.current = null
      setIsReady(false)
    }
  }, [])

  // Single tokenization
  const tokenize = useCallback(async (code: string): Promise<ThemedToken[][] | null> => {
    if (!workerRef.current || !isReady) {
      throw new Error('Worker not ready')
    }

    const id = ++requestIdRef.current

    return new Promise<ThemedToken[][] | null>((resolve, reject) => {
      pendingRef.current.set(id, { resolve, reject })

      workerRef.current!.postMessage({
        type: 'tokenize',
        id,
        payload: { code, language, theme }
      } as WorkerMessage)
    }) as Promise<ThemedToken[][] | null>
  }, [language, theme, isReady])

  // Chunk tokenization - processes multiple chunks in parallel
  const tokenizeChunks = useCallback(async (
    chunks: Array<{ code: string; chunkIndex: number }>
  ): Promise<Map<number, ThemedToken[][]>> => {
    if (!workerRef.current || !isReady) {
      throw new Error('Worker not ready')
    }

    const results = new Map<number, ThemedToken[][]>()

    const promises = chunks.map(({ code, chunkIndex }) => {
      const id = ++requestIdRef.current

      return new Promise<{ chunkIndex: number; tokens: ThemedToken[][] }>((resolve, reject) => {
        pendingRef.current.set(id, { 
          resolve: (payload) => {
            // Handle both TokenizeResult and TokenizeChunkResult
            const typed = payload as unknown as TokenizeResult | TokenizeChunkResult
            resolve({ chunkIndex: 'chunkIndex' in typed ? typed.chunkIndex : chunkIndex, tokens: typed.tokens })
          }, 
          reject 
        })

        workerRef.current!.postMessage({
          type: 'tokenizeChunk',
          id,
          payload: { code, language, theme, chunkIndex }
        } as WorkerMessage)
      })
    })

    const chunkResults = await Promise.all(promises)
    chunkResults.forEach(({ chunkIndex, tokens }) => {
      results.set(chunkIndex, tokens)
    })

    return results
  }, [language, theme, isReady])

  const terminate = useCallback(() => {
    workerRef.current?.terminate()
    workerRef.current = null
    setIsReady(false)
  }, [])

  return { tokenize, tokenizeChunks, isReady, error, terminate }
}

// Simpler hook for single-shot tokenization (used by DiffLines compact mode)
export function useShikiTokenize() {
  const { tokenize, isReady, error } = useShikiWorker({ language: 'txt' as BundledLanguage })
  
  return { tokenize, isReady, error }
}