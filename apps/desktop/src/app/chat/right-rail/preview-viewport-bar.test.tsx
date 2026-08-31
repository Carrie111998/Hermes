import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { VIEWPORT_PRESETS } from '@/lib/preview-viewport'

import { PreviewViewportBar } from './preview-viewport-bar'

const DESKTOP = VIEWPORT_PRESETS.find(preset => preset.id === 'desktop')!
const PHONE = VIEWPORT_PRESETS.find(preset => preset.id === 'phone')!

function bar(props: Partial<Parameters<typeof PreviewViewportBar>[0]> = {}) {
  const onChange = vi.fn()
  render(<PreviewViewportBar onChange={onChange} open scale={1} viewport={null} {...props} />)

  return onChange
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('presets', () => {
  it('renders nothing while closed', () => {
    render(<PreviewViewportBar onChange={vi.fn()} open={false} scale={1} viewport={null} />)
    expect(screen.queryByText('Fit')).toBeNull()
  })

  it('picks a size', () => {
    const onChange = bar()
    screen.getByText('Desktop').click()
    expect(onChange).toHaveBeenCalledWith(DESKTOP)
  })

  it('goes back to filling the pane', () => {
    const onChange = bar({ viewport: DESKTOP })
    screen.getByText('Fit').click()
    // null is the behaviour that existed before emulation, and stays default.
    expect(onChange).toHaveBeenCalledWith(null)
  })
})

describe('free values', () => {
  it('takes a typed size on Enter', () => {
    const onChange = bar()
    fireEvent.change(screen.getByLabelText('Viewport width'), { target: { value: '375' } })
    fireEvent.change(screen.getByLabelText('Viewport height'), { target: { value: '812' } })
    fireEvent.keyDown(screen.getByLabelText('Viewport height'), { key: 'Enter' })

    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ height: 812, width: 375 }))
  })

  it('takes it on blur too, since Enter is not the only way people leave a field', () => {
    const onChange = bar()
    const width = screen.getByLabelText('Viewport width')
    fireEvent.change(width, { target: { value: '1024' } })
    fireEvent.blur(width)
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ width: 1024 }))
  })

  it('keeps the other edge when only one is given', () => {
    const onChange = bar({ viewport: DESKTOP })
    const width = screen.getByLabelText('Viewport width')
    fireEvent.change(width, { target: { value: '1100' } })
    fireEvent.blur(width)
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ height: DESKTOP.height, width: 1100 }))
  })

  it('does nothing when neither box holds a number', () => {
    const onChange = bar()
    const width = screen.getByLabelText('Viewport width')
    fireEvent.change(width, { target: { value: 'abc' } })
    fireEvent.blur(width)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('fills the boxes from the active size, so one edge can be nudged', () => {
    bar({ viewport: PHONE })
    expect((screen.getByLabelText('Viewport width') as HTMLInputElement).value).toBe(String(PHONE.width))
    expect((screen.getByLabelText('Viewport height') as HTMLInputElement).value).toBe(String(PHONE.height))
  })
})

describe('rotate', () => {
  it('swaps the edges', () => {
    const onChange = bar({ viewport: PHONE })
    screen.getByLabelText('Rotate').click()
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ height: PHONE.width, width: PHONE.height }))
  })

  it('is unavailable while filling the pane, which has no edges to swap', () => {
    bar()
    expect((screen.getByLabelText('Rotate') as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('readout', () => {
  it('shows the zoom when the page had to be shrunk to fit', () => {
    bar({ scale: 0.347, viewport: DESKTOP })
    expect(screen.getByText(/1440×900 · 35%/)).toBeTruthy()
  })

  it('says mobile, because that changes how the page lays itself out', () => {
    bar({ scale: 1, viewport: PHONE })
    expect(screen.getByText(/mobile/)).toBeTruthy()
  })

  it('reports no size at all while filling the pane', () => {
    bar()
    // The bare × between the two inputs is a separator, not a readout.
    expect(screen.queryByText(/\d+×\d+/)).toBeNull()
  })
})
