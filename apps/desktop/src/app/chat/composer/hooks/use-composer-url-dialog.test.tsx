import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useComposerUrlDialog } from './use-composer-url-dialog'

describe('useComposerUrlDialog session scope', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('closes and clears an A dialog when the composer scope changes to B', () => {
    const onAddUrl = vi.fn()

    const hook = renderHook(
      ({ disabled, scopeKey }: { disabled: boolean; scopeKey: string }) =>
        useComposerUrlDialog({ disabled, insertText: vi.fn(), onAddUrl, scopeKey }),
      { initialProps: { disabled: false, scopeKey: 'session-a' } }
    )

    act(() => {
      hook.result.current.openUrlDialog()
      hook.result.current.setUrlValue('https://a.example')
    })

    expect(hook.result.current.urlOpen).toBe(true)
    expect(hook.result.current.urlValue).toBe('https://a.example')

    hook.rerender({ disabled: true, scopeKey: 'session-b' })

    expect(hook.result.current.urlOpen).toBe(false)
    expect(hook.result.current.urlValue).toBe('')
  })

  it('does not submit a URL while composer actions are fenced', () => {
    const onAddUrl = vi.fn()

    const hook = renderHook(() =>
      useComposerUrlDialog({ disabled: true, insertText: vi.fn(), onAddUrl, scopeKey: 'session-a' })
    )

    act(() => hook.result.current.setUrlValue('https://blocked.example'))
    act(() => hook.result.current.submitUrl())

    expect(onAddUrl).not.toHaveBeenCalled()
  })
})
