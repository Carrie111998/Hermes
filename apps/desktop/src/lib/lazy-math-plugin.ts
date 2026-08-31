import { useEffect, useMemo, useState } from 'react'

import type { createMemoizedMathPlugin } from '@/lib/katex-memo'

export type MathPlugin = ReturnType<typeof createMemoizedMathPlugin>

interface MathPluginModule {
  createMemoizedMathPlugin: typeof createMemoizedMathPlugin
}

type MathPluginImporter = () => Promise<MathPluginModule>

export interface LazyMathPluginLoader {
  load: (markdown: string) => Promise<MathPlugin | undefined>
  peek: (markdown: string) => MathPlugin | undefined
}

const MATH_FENCE_RE =
  /(?:^|\r?\n)[ \t]{0,3}(?:>[ \t]*)*(?:(?:[-+*]|\d+[.)])[ \t]+)?[ \t]*(?:`{3,}|~{3,})[ \t]*(?:latex|math|tex)(?=[\s,{]|$)/i

const NORMALIZED_MATH_MARKER_RE = /\[(?:\/?)(?:inline|math)\]|\\(?:\(|\[|begin\{)/i

/** Cheap admission check used before the KaTeX module is requested. */
export function hasRenderableMath(markdown: string): boolean {
  // False positives only load a deferred chunk; false negatives expose raw
  // delimiters and break rendering. Admit any dollar/custom-LaTeX marker, even
  // inside code, and let remark-math decide whether it is actual math.
  return markdown.includes('$') || NORMALIZED_MATH_MARKER_RE.test(markdown) || MATH_FENCE_RE.test(markdown)
}

export function createLazyMathPluginLoader(
  importer: MathPluginImporter = () => import('@/lib/katex-memo')
): LazyMathPluginLoader {
  let completed: MathPlugin | undefined
  let pending: Promise<MathPlugin> | undefined

  const peek = (markdown: string) => (hasRenderableMath(markdown) ? completed : undefined)

  return {
    load(markdown) {
      if (!hasRenderableMath(markdown)) {
        return Promise.resolve(undefined)
      }

      if (completed) {
        return Promise.resolve(completed)
      }

      if (pending) {
        return pending
      }

      const operation = importer().then(module => {
        completed = module.createMemoizedMathPlugin({ singleDollarTextMath: true })
        pending = undefined

        return completed
      })

      pending = operation

      return operation
    },
    peek
  }
}

export const lazyMathPluginLoader = createLazyMathPluginLoader()

export function useLazyMathPlugin(
  markdown: string,
  loader: LazyMathPluginLoader = lazyMathPluginLoader
): MathPlugin | undefined {
  const eligible = useMemo(() => hasRenderableMath(markdown), [markdown])
  const [plugin, setPlugin] = useState<MathPlugin | undefined>(() => loader.peek(markdown))

  useEffect(() => {
    if (!eligible) {
      return undefined
    }

    let active = true
    const cached = loader.peek(markdown)

    if (cached) {
      setPlugin(cached)

      return undefined
    }

    void loader
      .load(markdown)
      .then(loaded => {
        if (active && loaded) {
          setPlugin(loaded)
        }
      })
      .catch(() => undefined)

    return () => {
      active = false
    }
  }, [eligible, loader, markdown])

  return eligible ? plugin : undefined
}
