import { cleanup, render } from '@testing-library/react'
import { DndContext } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { afterEach, describe, expect, it } from 'vitest'

import { useSortableBindings } from './reorderable-list'

afterEach(cleanup)

// Regression test for the swallowed-Space bug: PR #82373 spread the full dnd-kit
// handle ({...dragHandleProps} — attributes + keyboard + pointer activators)
// onto the whole row SHELL. That made the shell itself a focusable drag
// activator: Space/Enter pressed while the shell (or a control inside it) had
// focus started keyboard drags, and while a drag session was active dnd-kit's
// KeyboardSensor arms a window keydown listener whose default `end` codes
// include Space — swallowing the keystroke in ANY text input (session rename
// dialog, first composer keystroke after launch).
//
// The split contract: the grabber keeps the FULL handle (keyboard reorder
// accessibility), the shell gets ONLY the pointer activator.
function Probe({ id }: { id: string }) {
  const bindings = useSortableBindings(id)

  return (
    <div>
      <pre data-testid="grabber-keys" data-keys={Object.keys(bindings.dragHandleProps ?? {}).join(',')} />
      <pre data-testid="shell-keys" data-keys={Object.keys(bindings.shellDragProps ?? {}).join(',')} />
    </div>
  )
}

function renderProbe() {
  return render(
    <DndContext>
      <SortableContext items={['a']} strategy={verticalListSortingStrategy}>
        <Probe id="a" />
      </SortableContext>
    </DndContext>
  )
}

describe('useSortableBindings', () => {
  it('keeps the full handle — keyboard activator + focusable attributes — on the grabber', () => {
    const { getByTestId } = renderProbe()

    const grabberKeys = (getByTestId('grabber-keys').dataset.keys ?? '').split(',')
    expect(grabberKeys).toContain('onKeyDown')
    expect(grabberKeys).toContain('onPointerDown')
  })

  it('gives the row shell only the pointer activator — no keyboard activator, no role/tabIndex', () => {
    const { getByTestId } = renderProbe()

    // The shell must never see the keyboard activator: Space/Enter pressed
    // while a shell descendant has focus would otherwise reach dnd-kit's
    // KeyboardSensor and be consumed by its default start/end codes. The
    // attributes (role=button / tabIndex) never enter shellDragProps either,
    // so the shell is not a focusable drag activator.
    expect((getByTestId('shell-keys').dataset.keys ?? '').split(',')).toEqual(['onPointerDown'])
  })
})
