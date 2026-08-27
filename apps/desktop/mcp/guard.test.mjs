import test from 'node:test'
import assert from 'node:assert/strict'
import { assertTargetAttested, canon } from './guard.mjs'

// Fake CDP whose Runtime.evaluate returns the given descriptor dataRoot.
const fakeCdp = (dataRoot) => ({
  eval: async (expr) => {
    // guard.mjs evaluates: globalThis.__DEBUG_MCP_INSTANCE__ ? ...dataRoot : null
    if (typeof dataRoot === 'string') return dataRoot
    return dataRoot
  }
})

test('canon resolves equivalent paths to the same string', () => {
  assert.equal(canon('/tmp/x'), canon('/tmp/./x'))
  assert.equal(canon('/tmp/sb'), canon('/tmp/sb/'))
})

test('refuses when EXPECTED_HOME is unset', async () => {
  await assert.rejects(
    () => assertTargetAttested(fakeCdp('/tmp/sb'), { expectedHome: '', defaultHome: '/real' }),
    /REFUSED/
  )
})

test('refuses when target exposes no descriptor (null dataRoot)', async () => {
  await assert.rejects(
    () => assertTargetAttested(fakeCdp(null), { expectedHome: '/tmp/sb', defaultHome: '/real' }),
    /no debug-instance descriptor/
  )
})

test('P1-1: refuses when declared sandbox != realized target home (attacker lies)', async () => {
  // Real dev Desktop on ~/.hermes; MCP declares a fake /tmp/sandbox.
  await assert.rejects(
    () =>
      assertTargetAttested(fakeCdp('/real/home'), {
        expectedHome: '/tmp/fake-sandbox',
        defaultHome: '/real/home'
      }),
    /does not match declared/
  )
})

test('P1-1: allows when target home matches declared (canonicalized)', async () => {
  // declared '/tmp/./sb' must match realized '/tmp/sb/'
  await assert.doesNotReject(
    () =>
      assertTargetAttested(fakeCdp('/tmp/sb/'), {
        expectedHome: '/tmp/./sb',
        defaultHome: '/real/home'
      })
  )
})

test('P1-1: incident shape — target fell back to real home → refused', async () => {
  // Electron instance had only HERMES_DESKTOP_USER_DATA_DIR and silently used ~/.hermes.
  await assert.rejects(
    () =>
      assertTargetAttested(fakeCdp('/Users/me/.hermes'), {
        expectedHome: '/tmp/sandbox',
        defaultHome: '/Users/me/.hermes'
      }),
    /does not match declared/
  )
})
