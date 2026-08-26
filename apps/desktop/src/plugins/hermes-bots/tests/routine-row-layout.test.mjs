import assert from 'node:assert/strict'
import test from 'node:test'
import { JOB, collect, findRow, hasAll, renderRoutineRow } from './routine-row-test-harness.mjs'

test('metadata keeps next-run visible and gives schedule the truncation budget', () => {
  const elements = collect(renderRoutineRow(JOB, { relativeTime: () => 'in 4 days' }))
  const metadata = findRow(elements, 'flex', 'flex-wrap', 'pl-3.5')
  const schedule = findRow(elements, 'inline-flex', 'flex-1', 'rounded-full')
  const scheduleText = elements.find(
    element => element.type === 'span' && hasAll(element, 'truncate') && element.props?.title === 'Daily'
  )
  const nextRun = elements.find(element => element.props?.children === 'next in 4 days')

  assert.ok(metadata, 'metadata line must render')
  assert.ok(hasAll(metadata, 'min-w-0'), 'metadata line must stay bounded')
  assert.ok(schedule, 'schedule pill must render')
  assert.ok(hasAll(schedule, 'min-w-0', 'max-w-full'), 'schedule must own the flexible width budget')
  assert.ok(scheduleText, 'schedule text must truncate and preserve its full tooltip')
  assert.ok(nextRun, 'next-run label must render')
  assert.ok(hasAll(nextRun, 'shrink-0', 'whitespace-nowrap', 'max-w-full', 'truncate'))
})

test('paused jobs keep the same bounded timing slot', () => {
  const elements = collect(renderRoutineRow({ ...JOB, enabled: false, next_run_at: null }))
  const paused = elements.find(element => element.props?.children === 'paused')

  assert.ok(paused, 'paused timing label must render')
  assert.ok(hasAll(paused, 'shrink-0', 'whitespace-nowrap', 'max-w-full', 'truncate'))
})
