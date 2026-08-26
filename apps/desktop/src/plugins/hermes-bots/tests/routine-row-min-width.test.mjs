import assert from 'node:assert/strict'
import test from 'node:test'
import { JOB, collect, findRow, hasAll, renderRoutineRow } from './routine-row-test-harness.mjs'

// RoutineRow narrow-pane layout contract (#91623): the Routines pane docks
// at 250px. Every grid/flex node in the width chain must carry min-w-0 so
// the title and metadata shrink rather than pushing controls past the clip.

test('row root grid carries min-w-0 so the row can shrink inside the pane', () => {
  const elements = collect(renderRoutineRow(JOB))
  const root = findRow(elements, 'grid')
  assert.ok(root, 'row root must be a grid')
  assert.ok(hasAll(root, 'min-w-0'), 'row root grid must be min-w-0')
})

test('title/controls line shrinks while Switch remains visible', () => {
  const elements = collect(renderRoutineRow(JOB))
  const line = elements.find(element => hasAll(element, 'flex', 'gap-2') && !hasAll(element, 'flex-wrap'))
  assert.ok(line, 'title/controls line must be a flex row')
  assert.ok(hasAll(line, 'min-w-0'), 'title/controls line must be min-w-0')

  const title = findRow(elements, 'flex-1', 'truncate')
  assert.ok(title, 'job title must be flex-1 truncate')
  assert.ok(hasAll(title, 'min-w-0'), 'title must carry min-w-0')
  assert.ok(
    elements.some(element => element.type === 'Switch'),
    'Switch must render'
  )
})

test('metadata line carries min-w-0 so wrapped timing stays inside the pane', () => {
  const elements = collect(renderRoutineRow(JOB))
  const metadata = findRow(elements, 'flex', 'flex-wrap', 'pl-3.5')
  assert.ok(metadata, 'metadata line must be a flex row')
  assert.ok(hasAll(metadata, 'min-w-0'), 'metadata line must be min-w-0')
})

test('delete button is shrink-0 so its icon is never crushed', () => {
  const elements = collect(renderRoutineRow(JOB))
  const remove = findRow(elements, 'size-5')
  assert.ok(remove, 'delete button must render')
  assert.ok(hasAll(remove, 'shrink-0'), 'delete button must be shrink-0')
})

test('legacy warning strip carries min-w-0', () => {
  const legacyJob = {
    ...JOB,
    prompt_preview:
      'You are running the scheduled routine "Daily digest" for agent \'researcher\'. Execute it AS that agent...'
  }
  const elements = collect(renderRoutineRow(legacyJob))
  const strip = findRow(elements, 'px-2', 'py-1.5')
  assert.ok(strip, 'legacy warning strip must render')
  assert.ok(hasAll(strip, 'min-w-0'), 'legacy warning strip must be min-w-0')
})
