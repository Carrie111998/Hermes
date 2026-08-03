import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useRef, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SearchField } from '@/components/ui/search-field'

import { SidebarSearchField } from './index'

// SCOPE, stated up front: jsdom has no layout and cannot reproduce OS hit
// testing. Nothing here proves Electron's compositor lets a native press reach
// the field -- only a live run can. What these tests do prove is the two things
// the live failure needed:
//
//   1. the no-drag carve-out is on the full-width ROW, not on SearchField's
//      content-sized `inline-flex` container; and
//   2. a press anywhere in that row that is not already an interactive control
//      moves focus into the input.
//
// The second is the actual repair. The field is content-sized, so most of the
// visible row is container padding: without the handler a press there left
// focus on <body> and the next keystroke fell through to the session-list key
// handlers -- observed live as a session-row context menu opening instead of
// the query changing.

const NO_DRAG = '[-webkit-app-region:no-drag]'

/** Nearest ancestor-or-self carrying the explicit no-drag class. `classList`
 *  matches the raw token, which avoids escaping the arbitrary-property name. */
function noDragBoundary(node: Element | null): Element | null {
  for (let el = node; el; el = el.parentElement) {
    if (el.classList.contains(NO_DRAG)) {
      return el
    }
  }

  return null
}

/** Mounts the shipped composition with a live ref and controlled value, the
 *  way ChatSidebar wires it. */
function renderRow(initial = '') {
  const onChange = vi.fn()

  function Harness() {
    const inputRef = useRef<HTMLInputElement>(null)
    const [value, setValue] = useState(initial)

    return (
      <SidebarSearchField
        aria-label="Search sessions"
        inputRef={inputRef}
        onChange={next => {
          onChange(next)
          setValue(next)
        }}
        placeholder="Search sessions"
        value={value}
      />
    )
  }

  const utils = render(<Harness />)
  const input = screen.getByRole('textbox') as HTMLInputElement

  return {
    ...utils,
    field: input.parentElement!,
    input,
    onChange,
    row: utils.container.firstElementChild!
  }
}

afterEach(() => {
  cleanup()
})

describe('sidebar search row: drag carve-out', () => {
  it('puts the carve-out on the full-width row, not on the content-sized field', () => {
    const { field, input, row } = renderRow('query')

    expect(row.classList.contains(NO_DRAG)).toBe(true)
    // The row is a plain block container. If the carve-out ever moves back onto
    // SearchField's root it lands on an `inline-flex` box sized to the glyph +
    // typed text, which is what left most of the visible row uncovered.
    expect(row.className).not.toContain('inline-flex')
    expect(field.className).toContain('inline-flex')
    expect(row.contains(field)).toBe(true)

    // Nearest carve-out for every hit target in the row is the row itself.
    expect(noDragBoundary(input)).toBe(row)
    expect(noDragBoundary(row.querySelector('svg'))).toBe(row)
    expect(noDragBoundary(screen.getByRole('button'))).toBe(row)
  })

  it('finds no boundary on a bare SearchField — the assertion above can fail', () => {
    // Negative control. `SearchField` is shared with pages and overlays that
    // sit nowhere near a drag strip, so it carries no carve-out of its own.
    render(<SearchField onChange={() => {}} placeholder="Search" value="query" />)

    expect(noDragBoundary(screen.getByRole('textbox'))).toBeNull()
  })
})

describe('sidebar search row: click anywhere to focus', () => {
  it('focuses the input on mousedown in the row padding', () => {
    const { input, row } = renderRow()

    expect(window.document.activeElement).not.toBe(input)

    // False = preventDefault fired: the row refuses focus so the explicit
    // focus() lands instead of being overwritten.
    expect(fireEvent.mouseDown(row)).toBe(false)
    expect(window.document.activeElement).toBe(input)
  })

  it('focuses the input on mousedown on the field container and the glyph', () => {
    const { field, input, row } = renderRow()

    expect(fireEvent.mouseDown(field)).toBe(false)
    expect(window.document.activeElement).toBe(input)

    input.blur()
    expect(window.document.activeElement).not.toBe(input)

    // The glyph is pointer-events-none, so a real press retargets to the field
    // root; dispatching on it directly covers the same handler either way.
    expect(fireEvent.mouseDown(row.querySelector('svg')!)).toBe(false)
    expect(window.document.activeElement).toBe(input)
  })

  it('leaves the input its own native press and typing', () => {
    const { input, onChange } = renderRow()

    // True = not cancelled, so the browser still places the caret at the click.
    expect(fireEvent.mouseDown(input)).toBe(true)

    fireEvent.change(input, { target: { value: 'delegated' } })

    expect(onChange).toHaveBeenCalledWith('delegated')
    expect(input.value).toBe('delegated')
  })

  it('leaves the clear button its action and does not steal its focus', () => {
    const { input, onChange } = renderRow('delegated child')

    const clear = screen.getByRole('button')

    // The guard must skip buttons: no preventDefault, no focus hijack.
    expect(fireEvent.mouseDown(clear)).toBe(true)
    expect(window.document.activeElement).not.toBe(input)

    fireEvent.click(clear)

    expect(onChange).toHaveBeenCalledWith('')
    expect(input.value).toBe('')
  })
})
