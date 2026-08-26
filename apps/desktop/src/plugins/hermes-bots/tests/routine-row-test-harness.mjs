import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function slice(from, to) {
  const start = source.indexOf(from)
  const end = source.indexOf(to, start)
  assert.ok(start >= 0 && end > start, `slice ${JSON.stringify(from)} must remain extractable`)
  return source.slice(start, end)
}

export function renderRoutineRow(job, { relativeTime = () => 'now' } = {}) {
  const helpers = slice('const BOT_TAG_RE = ', '\nasync function loadRoutines(')
  const scheduleLabel = slice('function scheduleLabel(', '\nfunction RoutineRow(')
  const row = slice('function RoutineRow(', '\n// Structured schedule picker')
  const context = {
    jsx: (type, props) => ({ type, props: props || {} }),
    jsxs: (type, props) => ({ type, props: props || {} }),
    useState: initial => [initial, () => {}],
    cn: (...parts) => parts.filter(Boolean).join(' '),
    relativeTime,
    Switch: 'Switch',
    Tip: 'Tip',
    Codicon: 'Codicon',
    host: { request: async () => ({}), notifyError: () => {} },
    invalidateRoutineOwner: async () => {}
  }
  vm.runInNewContext(`${helpers}\n${scheduleLabel}\n${row}\nglobalThis.__row = RoutineRow;`, context)

  return context.__row({ job, owner: undefined })
}

export function collect(node, acc = []) {
  if (!node || typeof node !== 'object') {
    return acc
  }
  const { props } = node
  const entry = { type: node.type, props }
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

export const hasAll = (element, ...tokens) =>
  Boolean(element.tokens) && tokens.every(token => element.tokens.has(token))

export const findRow = (elements, ...tokens) => elements.find(element => hasAll(element, ...tokens))

export const JOB = {
  job_id: 'j1',
  name: '[bot:researcher] Daily digest',
  schedule: 'every 1440m',
  enabled: true,
  state: 'active',
  next_run_at: '2026-08-23T00:00:00Z'
}
