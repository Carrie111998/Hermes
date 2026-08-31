/**
 * Lazy loader for the KaTeX math plugin.
 *
 * `@/lib/katex-memo` statically imports `katex`. Measured on the built
 * renderer, that landed 253 KB on the cold boot graph and was the single
 * slowest chunk of the 105 (5.97 MB) the entry modulepreloads — paid by
 * every user on every launch, for a feature most conversations never reach.
 *
 * This is the same treatment `useCodePlugin` gives `@streamdown/code` (which
 * drags in all of shiki) and `LazyShiki` gives `shiki-block.tsx`. Keep the
 * dynamic `import()` here as the ONLY route to `katex-memo` from anything the
 * entry graph reaches — a static import from a boot-path module puts katex
 * straight back into the preload set.
 *
 * The plugin instance is module-cached and shared across every consumer: it
 * is stateless beyond its internal LRU, so a per-caller instance would just
 * split the cache and re-render equations that a sibling already rendered.
 *
 * Until the chunk lands, `$x^2$` renders as literal text and then typesets.
 * That swap happens once per process, on the first markdown mount that needs
 * it, mirroring how fenced code renders plain before shiki arrives.
 */

import { useEffect, useState } from 'react'

// Type-only — erased at build time, so it does NOT pull katex onto the graph.
// The runtime route stays the dynamic import() below.
import type { createMemoizedMathPlugin } from './katex-memo'

export type MathPlugin = ReturnType<typeof createMemoizedMathPlugin>

let cache: MathPlugin | null = null

export function useMathPlugin(): MathPlugin | null {
  const [plugin, setPlugin] = useState(cache)

  useEffect(() => {
    if (plugin) {
      return
    }

    let cancelled = false

    void import('./katex-memo').then(({ createMemoizedMathPlugin }) => {
      // `singleDollarTextMath: true` enables `$x^2$` for inline math (the
      // de-facto LLM convention). The default false-setting only accepts
      // `$$...$$`.
      cache ??= createMemoizedMathPlugin({ singleDollarTextMath: true })

      if (!cancelled) {
        setPlugin(cache)
      }
    })

    return () => {
      cancelled = true
    }
  }, [plugin])

  return plugin
}
