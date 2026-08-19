import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PreviewElementPickControl } from './preview-element-pick-control'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('PreviewElementPickControl', () => {
  it('toggles pick mode from the inspect glyph', () => {
    const onToggle = vi.fn()
    const rendered = render(<PreviewElementPickControl onToggle={onToggle} picking={false} />)

    fireEvent.click(rendered.getByRole('button', { name: 'Select page element for chat' }))
    expect(onToggle).toHaveBeenCalledOnce()
  })

  it('labels the button as cancel while picking', () => {
    const rendered = render(<PreviewElementPickControl onToggle={vi.fn()} picking />)

    expect(rendered.getByRole('button', { name: 'Cancel element pick' }).getAttribute('aria-pressed')).toBe('true')
  })
})
