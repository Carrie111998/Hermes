import test from 'node:test'
import assert from 'node:assert/strict'
import { createReadTools } from './read.mjs'

// Capture the expression evalBounded would run, return a fake result.
function fakeConnect(result) {
  const calls = []
  const connect = async () => ({
    eval: async (expression) => {
      calls.push(expression)
      return typeof result === 'function' ? result(expression) : result
    }
  })
  return { connect, calls }
}

const deps = (result) => {
  const { connect, calls } = fakeConnect(result)
  const t = createReadTools({ connect, MAX_TEXT: 80, MAX_NODES: 20, MAX_EVAL: 4000, port: 9222, allowAct: false })
  return { ...t, calls }
}

test('BUG #6 (RED): query with limit:0 falls back to the cap instead of returning 0 nodes', async () => {
  const { query, calls } = deps(() => '[]')
  await query({ selector: 'div', limit: 0 })
  assert.ok(calls[0].includes('20'), 'expression must slice to MAX_NODES (20), got: ' + calls[0].match(/slice\(0, (\d+)\)/)?.[1])
})

test('BUG #4 (RED): query whitespace regex must be a single-escaped \\s+ in the sent expression', async () => {
  const { query, calls } = deps(() => '[]')
  await query({ selector: 'div' })
  assert.ok(calls[0].includes('/\\s+/g'), 'expression must contain /\\s+/g exactly')
  assert.ok(!calls[0].includes('\\\\s+'), 'expression must NOT contain a double backslash')
})

test('BUG #9 (RED): empty/whitespace selector throws a friendly error before eval', async () => {
  const { inspect, query } = deps(() => '[]')
  await assert.rejects(() => inspect({ selector: '' }), /selector required/i)
  await assert.rejects(() => query({ selector: '   ' }), /selector required/i)
})

test('#10: evalBounded truncation reports the omitted length', async () => {
  const long = 'x'.repeat(5000)
  const { evalBounded } = deps(long)
  const out = await evalBounded('1')
  assert.ok(out.startsWith('x'.repeat(4000).slice(0, 10)), 'starts with the kept prefix')
  assert.ok(/\[\+1000 chars truncated\]/.test(out), 'must report how many chars were cut, got: ' + out.slice(-40))
})

test('A2 (RED): evalBounded returns "null" when the renderer eval is undefined', async () => {
  const { evalBounded } = deps(undefined)
  assert.equal(await evalBounded('void 0'), 'null')
})
