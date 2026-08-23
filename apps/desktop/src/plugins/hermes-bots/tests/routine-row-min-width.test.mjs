import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// RoutineRow narrow-pane layout contract (#91623):
// The Routines (Cronjobs) pane docks right at 250px. RoutineRow is a
// display:grid node inside the pane's grid list; grid items default to
// min-width:auto, so the row could not shrink below its min-content width.
// A nowrap job title pinned the row at ~284px inside the 250px pane, the
// overflow-hidden ScrollArea clipped the right edge, and the enable/disable
// Switch + delete button became invisible (title truncation also never
// engaged). Every grid/flex node in the row's width chain must carry
// min-w-0 so the title and metadata can actually shrink.
//
// These tests render the real RoutineRow (extracted from plugin.js, with
// the React runtime and SDK components stubbed) and assert the contract on
// the resulting JSX tree — not on source formatting. Tailwind class order,
// cn() composition, or whitespace can change freely without false reds; a
// real regression in the width chain (dropped min-w-0 / shrink-0) fails.

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function slice(from, to) {
  const start = source.indexOf(from)
  const end = source.indexOf(to, start)
  assert.ok(start >= 0 && end > start, `slice ${JSON.stringify(from)} must remain extractable`)
  return source.slice(start, end)
}

function renderRoutineRow(job) {
  // Real helper logic rides along with the row; only the React runtime,
  // SDK components, and host bridge are stubbed.
  const helpers = slice('const BOT_TAG_RE = ', '\nasync function loadRoutines(')
  const scheduleLabel = slice('function scheduleLabel(', '\nfunction RoutineRow(')
  const row = slice('function RoutineRow(', '\n// Structured schedule picker')

  const context = {
    jsx: (type, props) => ({ type, props: props || {} }),
    jsxs: (type, props) => ({ type, props: props || {} }),
    useState: initial => [initial, () => {}],
    cn: (...parts) => parts.filter(Boolean).join(' '),
    relativeTime: () => 'now',
    Switch: 'Switch',
    Tip: 'Tip',
    Codicon: 'Codicon',
    host: { request: async () => ({}), notifyError: () => {} },
    invalidateRoutineOwner: async () => {}
  }
  vm.runInNewContext(
    `${helpers}\n${scheduleLabel}\n${row}\nglobalThis.__row = RoutineRow;`,
    context
  )
  // RoutineRow takes { job, owner } since the bot-owner routing rework;
  // the width-chain contract does not depend on the owner value.
  return context.__row({ job, owner: undefined })
}

// Flatten the JSX tree into nodes. Nodes with a className get
// { type, tokens } (tokens = the cn()-joined class set); classless nodes
// (Switch etc.) are still listed so their presence can be asserted.
function collect(node, acc = []) {
  if (!node || typeof node !== 'object') {
    return acc
  }
  const { props } = node
  const entry = { type: node.type }
  if (typeof props?.className === 'string') {
    entry.tokens = new Set(props.className.split(/\s+/))
  }
  acc.push(entry)
  const children = props?.children
  if (Array.isArray(children)) {
    for (const child of children) {
      collect(child, acc)
    }
  } else {
    collect(children, acc)
  }
  return acc
}

const hasAll = (el, ...tokens) => !!el.tokens && tokens.every(t => el.tokens.has(t))
const findRow = (els, ...tokens) => els.find(el => hasAll(el, ...tokens))

const JOB = {
  job_id: 'j1',
  name: '[bot:researcher] Daily digest',
  schedule: 'every 60m',
  enabled: true,
  state: 'active',
  next_run_at: '2026-08-23T00:00:00Z'
}

test('row root grid carries min-w-0 so the row can shrink inside the pane', () => {
  const els = collect(renderRoutineRow(JOB))
  const root = findRow(els, 'grid')
  assert.ok(root, 'row root must be a grid')
  assert.ok(hasAll(root, 'min-w-0'), 'row root grid must be min-w-0')
})

test('title/controls flex line carries min-w-0 (title truncate engages, Switch stays visible)', () => {
  const els = collect(renderRoutineRow(JOB))
  // The title line is the first flex row; the metadata line also uses
  // justify-between, so require its absence to pin the right line.
  const line = els.find(el => hasAll(el, 'flex', 'gap-2') && !hasAll(el, 'justify-between'))
  assert.ok(line, 'title/controls line must be a flex row')
  assert.ok(hasAll(line, 'min-w-0'), 'title/controls line must be min-w-0')

  const title = findRow(els, 'flex-1', 'truncate')
  assert.ok(title, 'job title span must be flex-1 truncate')
  assert.ok(hasAll(title, 'min-w-0'), 'title must carry min-w-0 for truncation to engage')

  assert.ok(els.some(el => el.type === 'Switch'), 'enable/disable Switch must render')
})

test('metadata line carries min-w-0 so the next-run label can truncate, not clip', () => {
  const els = collect(renderRoutineRow(JOB))
  const meta = findRow(els, 'flex', 'justify-between', 'pl-3.5')
  assert.ok(meta, 'metadata line must be a flex row')
  assert.ok(hasAll(meta, 'min-w-0'), 'metadata line must be min-w-0')
})

test('delete button is shrink-0 so the icon is never crushed in narrow panes', () => {
  const els = collect(renderRoutineRow(JOB))
  const del = findRow(els, 'size-5')
  assert.ok(del, 'delete button must render')
  assert.ok(hasAll(del, 'shrink-0'), 'delete button must be shrink-0')
})

test('legacy-routine warning strip carries min-w-0 (same grid item class of bug)', () => {
  const legacyJob = {
    ...JOB,
    prompt_preview:
      'You are running the scheduled routine "Daily digest" for agent \'researcher\'. Execute it AS that agent...'
  }
  const els = collect(renderRoutineRow(legacyJob))
  const strip = findRow(els, 'px-2', 'py-1.5')
  assert.ok(strip, 'legacy warning strip must render for legacy jobs')
  assert.ok(hasAll(strip, 'min-w-0'), 'legacy warning strip must be min-w-0')
})
