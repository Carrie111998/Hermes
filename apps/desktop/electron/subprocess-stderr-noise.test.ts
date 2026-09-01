import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  createBenignStderrSink,
  filterBenignDarwinSubprocessStderr,
  isBenignDarwinMallocStackLoggingLine
} from './subprocess-stderr-noise'

const EXACT =
  "MallocStackLogging: can't turn off malloc stack logging because it was not enabled."

const WITH_PID = `Python(12345) ${EXACT}`

// --- predicate ---------------------------------------------------------------

test('predicate matches the exact line and the Python(pid) variant on darwin', () => {
  assert.equal(isBenignDarwinMallocStackLoggingLine(EXACT, 'darwin'), true)
  assert.equal(isBenignDarwinMallocStackLoggingLine(WITH_PID, 'darwin'), true)
  assert.equal(
    isBenignDarwinMallocStackLoggingLine(`python(56273) ${EXACT}`, 'darwin'),
    true
  )
})

test('predicate is a no-op on linux and win32', () => {
  assert.equal(isBenignDarwinMallocStackLoggingLine(EXACT, 'linux'), false)
  assert.equal(isBenignDarwinMallocStackLoggingLine(EXACT, 'win32'), false)
})

test('predicate rejects near-misses', () => {
  for (const line of [
    `ERROR: ${EXACT}`,
    `${EXACT} (see docs)`,
    EXACT.slice(0, -1),
    'MallocStackLogging: malloc stack logging enabled',
    'Python(abc) ' + EXACT,
    ''
  ]) {
    assert.equal(isBenignDarwinMallocStackLoggingLine(line, 'darwin'), false, line)
  }
})

// --- whole-string filter -------------------------------------------------------

test('string filter removes only the noise line', () => {
  assert.equal(
    filterBenignDarwinSubprocessStderr(`real\n${WITH_PID}\nelse\n`, 'darwin'),
    'real\nelse\n'
  )
})

test('string filter is a byte-identical no-op off darwin', () => {
  const text = `real\n${WITH_PID}\nelse\n`
  assert.equal(filterBenignDarwinSubprocessStderr(text, 'linux'), text)
})

// --- streaming sink: the Node-specific contract --------------------------------

function collect(platform) {
  const seen = []
  const sink = createBenignStderrSink(chunk => seen.push(chunk), platform)

  return { seen, sink }
}

test('sink drops a whole noise line delivered in one chunk', () => {
  const { seen, sink } = collect('darwin')
  sink(`real failure\n${EXACT}\nanother failure\n`)
  sink.flush()
  assert.equal(seen.join(''), 'real failure\nanother failure\n')
})

test('sink drops a noise line split across two chunks', () => {
  const { seen, sink } = collect('darwin')
  sink(`real\nPython(123) MallocStackLogging: can't turn off ma`)
  sink(`lloc stack logging because it was not enabled.\nkeep\n`)
  sink.flush()
  assert.equal(seen.join(''), 'real\nkeep\n')
})

test('sink drops multiple noise lines in one chunk', () => {
  const { seen, sink } = collect('darwin')
  sink(`${EXACT}\nmid\n${WITH_PID}\n`)
  sink.flush()
  assert.equal(seen.join(''), 'mid\n')
})

test('sink flushes an unterminated non-matching tail on close', () => {
  const { seen, sink } = collect('darwin')
  sink('partial line without newline')
  const rest = sink.flush()

  assert.equal(rest, 'partial line without newline')
})

test('sink flush discards an unterminated pure noise tail', () => {
  const { seen, sink } = collect('darwin')
  sink(EXACT)
  const rest = sink.flush()

  assert.equal(rest, '')
  assert.equal(seen.join(''), '')
})

test('sink passes everything through on linux with no line work', () => {
  const { seen, sink } = collect('linux')
  sink(`real\n${WITH_PID}\nno newline at end`)
  sink.flush()
  assert.equal(seen.join(''), `real\n${WITH_PID}\nno newline at end`)
})

test('sink keeps a line that merely contains the sentence inside a traceback', () => {
  const { seen, sink } = collect('darwin')
  sink(`ValueError: got "${EXACT}"\n`)
  sink.flush()
  assert.equal(seen.join(''), `ValueError: got "${EXACT}"\n`)
})
