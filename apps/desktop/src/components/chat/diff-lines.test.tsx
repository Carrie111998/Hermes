import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ErrorBoundary } from '@/components/error-boundary'

afterEach(() => {
  cleanup()
  vi.doUnmock('./syntax-diff')
  vi.resetModules()
})

const DIFF = [
  'diff --git a/file.ts b/file.ts',
  '--- a/file.ts',
  '+++ b/file.ts',
  '@@ -1,2 +1,2 @@',
  ' const a = 1',
  '-const b = 2',
  '+const b = 3'
].join('\n')

const WORKSPACE_FALLBACK_TEXT = 'workspace failed to render'

// The failure surfaces only from console.error noise, not from the assertion.
function renderQuietly(node: Parameters<typeof render>[0]) {
  const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined)

  try {
    return render(node)
  } finally {
    spy.mockRestore()
  }
}

describe('FileDiffPanel survives a failed lazy syntax-diff chunk', () => {
  it('degrades to the plain colored diff instead of taking down the surrounding boundary', async () => {
    // Keep the rejected lazy import local to this test instead of using a
    // hoisted file-wide mock. The latter can outlive this file's execution
    // and surface its rejection as an unhandled error in a sibling UI test.
    vi.doMock('./syntax-diff', () => {
      throw new Error(
        'Failed to fetch dynamically imported module: file:///Hermes.app/Contents/Resources/app.asar/dist/assets/syntax-diff-Bo0962zh.js'
      )
    })

    const { FileDiffPanel } = await import('./diff-lines')
    const { container } = renderQuietly(
      <ErrorBoundary fallback={() => <div>{WORKSPACE_FALLBACK_TEXT}</div>} label="workspace">
        <FileDiffPanel diff={DIFF} path="file.ts" />
      </ErrorBoundary>
    )

    // The rejection settles a tick after the initial Suspense-pending render
    // (which coincidentally shows the same plain text already) — give it
    // real time to propagate before asserting nothing regressed.
    await act(() => new Promise(resolve => setTimeout(resolve, 300)))

    expect(container.textContent).toContain('const a = 1')
    expect(container.textContent).toContain('const b = 2')
    expect(container.textContent).toContain('const b = 3')
    expect(container.textContent).not.toContain(WORKSPACE_FALLBACK_TEXT)
  })
})
