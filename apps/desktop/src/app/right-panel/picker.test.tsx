import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'

import { $fileBrowserOpen, setFileBrowserOpen } from '@/store/layout'
import { setCurrentCwdTransient } from '@/store/session'

import { RightPanelPicker } from './picker'

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  globalThis.ResizeObserver = TestResizeObserver as typeof ResizeObserver
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.setPointerCapture ??= () => undefined
  Element.prototype.releasePointerCapture ??= () => undefined
  HTMLElement.prototype.scrollIntoView ??= () => undefined
})

afterEach(() => {
  cleanup()
  setFileBrowserOpen(false)
  setCurrentCwdTransient('')
})

describe('right panel picker', () => {
  it('uses a persistent checkbox to open and close the independent Files panel', async () => {
    setCurrentCwdTransient('/tmp/project')
    setFileBrowserOpen(false)

    render(<RightPanelPicker trigger={<button type="button">Panels</button>} />)
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Panels' }), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse'
    })

    const files = await screen.findByRole('menuitemcheckbox', { name: 'File system' })

    expect(files.getAttribute('aria-checked')).toBe('false')

    fireEvent.click(files)
    expect($fileBrowserOpen.get()).toBe(true)
    expect(files.getAttribute('aria-checked')).toBe('true')

    fireEvent.click(files)
    expect($fileBrowserOpen.get()).toBe(false)
    expect(files.getAttribute('aria-checked')).toBe('false')
  })
})
