import { type ComponentProps } from 'react'

const { editor } = vi.hoisted(() => ({ editor: vi.fn() }))

vi.mock('@excalidraw/excalidraw', () => ({
  Excalidraw: (props: ComponentProps<'div'> & { initialData?: unknown }) => {
    editor(props)

    return <div data-testid="editor" />
  }
}))

vi.mock('@excalidraw/excalidraw/index.css', () => ({}))
import { act, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { createDrawingController, loadDrawing, onSceneChange, subscribe } = vi.hoisted(() => ({
  createDrawingController: vi.fn(),
  loadDrawing: vi.fn(),
  onSceneChange: vi.fn(),
  subscribe: vi.fn()
}))

vi.mock('./document', () => ({ createDrawingController, loadDrawing }))

import { ExcalidrawPane } from './ExcalidrawPane'
import type { ExcalidrawDocumentIdentity } from './identity'

const identity: ExcalidrawDocumentIdentity = {
  path: '/drawings/design.excalidraw',
  profile: 'default',
  runtime: 'local'
}

describe('ExcalidrawPane', () => {
  beforeEach(() => {
    loadDrawing.mockReset()
    createDrawingController.mockReset()
    loadDrawing.mockResolvedValue({
      identity,
      elements: [{ id: 'loaded' }],
      appState: { theme: 'dark' },
      files: { image: { id: 'image' } },
      envelope: {},
      fingerprint: 'fp'
    })
    createDrawingController.mockReturnValue({
      dispose: () => undefined,
      getState: () => ({
        status: 'ready',
        identity,
        elements: [{ id: 'loaded' }],
        appState: { theme: 'dark' },
        files: { image: { id: 'image' } }
      }),
      onSceneChange,
      subscribe
    })
    onSceneChange.mockReset()
    subscribe.mockReset()
  })

  it('does not load the editor before the pane mounts', async () => {
    expect(ExcalidrawPane).toBeTypeOf('function')
    expect(loadDrawing).not.toHaveBeenCalled()

    render(<ExcalidrawPane identity={identity} />)

    expect(await screen.findByText('Loading drawing…')).toBeTruthy()
    expect(loadDrawing).toHaveBeenCalledWith(identity)
  })

  it('passes loaded elements, app state, and files through initialData', async () => {
    render(<ExcalidrawPane identity={identity} />)

    await screen.findByTestId('editor')
    expect(editor).toHaveBeenLastCalledWith(
      expect.objectContaining({
        initialData: { appState: { theme: 'dark' }, elements: [{ id: 'loaded' }], files: { image: { id: 'image' } } }
      })
    )
  })
  it('does not rerender the editor for editor-originated controller updates', async () => {
    let listener: ((origin: 'editor' | 'external') => void) | undefined
    subscribe.mockImplementation((callback: (origin: 'editor' | 'external') => void) => {
      listener = callback

      return () => undefined
    })
    render(<ExcalidrawPane identity={identity} />)

    await screen.findByTestId('editor')
    const initialCalls = editor.mock.calls.length
    act(() => listener?.('editor'))
    expect(editor).toHaveBeenCalledTimes(initialCalls)
  })
  it('remounts an external replacement once with same-ID files and accepts the next edit', async () => {
    let listener: ((origin: 'editor' | 'external') => void) | undefined
    let state = {
      status: 'ready' as const,
      identity,
      elements: [{ id: 'loaded' }],
      appState: { theme: 'dark' },
      files: { image: { id: 'same', dataURL: 'old' } }
    }
    subscribe.mockImplementation((callback: (origin: 'editor' | 'external') => void) => {
      listener = callback

      return () => undefined
    })
    createDrawingController.mockReturnValue({
      dispose: () => undefined,
      getState: () => state,
      onSceneChange,
      subscribe
    })
    render(<ExcalidrawPane identity={identity} />)

    await screen.findByTestId('editor')
    const initialCalls = editor.mock.calls.length
    state = { ...state, elements: [{ id: 'external' }], files: { image: { id: 'same', dataURL: 'replacement' } } }
    listener?.('external')
    await vi.waitFor(() => expect(editor.mock.calls.length).toBe(initialCalls + 1))
    expect(editor).toHaveBeenLastCalledWith(
      expect.objectContaining({
        initialData: expect.objectContaining({
          elements: [{ id: 'external' }],
          files: { image: { id: 'same', dataURL: 'replacement' } }
        })
      })
    )

    editor.mock.calls.at(-1)?.[0].onChange?.([], {}, {})
    expect(onSceneChange).not.toHaveBeenCalled()
    editor.mock.calls.at(-1)?.[0].onChange?.([{ id: 'next' }], {}, {})
    expect(onSceneChange).toHaveBeenCalledWith([{ id: 'next' }], {}, {})
  })
})
