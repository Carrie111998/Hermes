import { act, renderHook, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { createLazyMathPluginLoader, hasRenderableMath, type MathPlugin, useLazyMathPlugin } from './lazy-math-plugin'

const plugin = { name: 'katex' as const, type: 'math' as const } as MathPlugin

describe('hasRenderableMath', () => {
  it.each([
    'Inline $x^2$ math',
    'Display $$E = mc^2$$',
    String.raw`Inline \(x + y\) math`,
    String.raw`Display \[x + y\] math`,
    '```math\nx^2\n```',
    '$ x$',
    '$a\nb$',
    '[/math]\nx^2\n[/math]',
    '````math title="equation"\r\nx^2\r\n`````'
  ])('detects supported math syntax in %s', text => {
    expect(hasRenderableMath(text)).toBe(true)
  })

  it('does not treat ordinary prose or code without math markers as renderable math', () => {
    expect(hasRenderableMath('ordinary prose')).toBe(false)
    expect(hasRenderableMath('`echo HOME`')).toBe(false)
  })
})

describe('createLazyMathPluginLoader', () => {
  it('does not import KaTeX for prose-only markdown', async () => {
    const importer = vi.fn()
    const loader = createLazyMathPluginLoader(importer)

    await expect(loader.load('ordinary prose')).resolves.toBeUndefined()
    expect(importer).not.toHaveBeenCalled()
  })

  it('deduplicates concurrent imports and reuses the completed plugin', async () => {
    const importer = vi.fn(async () => ({ createMemoizedMathPlugin: () => plugin }))
    const loader = createLazyMathPluginLoader(importer)

    const [first, second] = await Promise.all([loader.load('$x$'), loader.load('$$y$$')])

    expect(first).toBe(plugin)
    expect(second).toBe(plugin)
    await expect(loader.load('$z$')).resolves.toBe(plugin)
    expect(importer).toHaveBeenCalledOnce()
  })

  it('does not re-request a module URL that Chromium has already poisoned', async () => {
    const failure = new Error('chunk failed')
    const importer = vi.fn().mockRejectedValue(failure)
    const loader = createLazyMathPluginLoader(importer)

    await expect(loader.load('$x$')).rejects.toBe(failure)
    await expect(loader.load('$x$')).rejects.toBe(failure)
    expect(importer).toHaveBeenCalledOnce()
  })
})

describe('useLazyMathPlugin', () => {
  it('publishes the plugin after eligible markdown loads it', async () => {
    const importer = vi.fn(async () => ({ createMemoizedMathPlugin: () => plugin }))
    const loader = createLazyMathPluginLoader(importer)

    const { result, rerender } = renderHook(({ text }) => useLazyMathPlugin(text, loader), {
      initialProps: { text: 'ordinary prose' }
    })

    expect(result.current).toBeUndefined()
    expect(importer).not.toHaveBeenCalled()

    rerender({ text: 'Now $x$ appears' })

    await waitFor(() => expect(result.current).toBe(plugin))
    expect(importer).toHaveBeenCalledOnce()

    act(() => rerender({ text: 'prose again' }))
    expect(result.current).toBeUndefined()
  })
})
