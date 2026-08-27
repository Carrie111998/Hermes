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

// Fake CDP whose eval THROWS (renderer Runtime error / disconnect).
const throwingCdp = (msg = 'eval failed') => ({
  eval: async () => {
    throw new Error(msg)
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
  await assert.doesNotReject(
    () =>
      assertTargetAttested(fakeCdp('/tmp/sb/'), {
        expectedHome: '/tmp/./sb',
        defaultHome: '/real/home'
      })
  )
})

test('P1-1: incident shape — target fell back to real home → refused', async () => {
  await assert.rejects(
    () =>
      assertTargetAttested(fakeCdp('/Users/me/.hermes'), {
        expectedHome: '/tmp/sandbox',
        defaultHome: '/Users/me/.hermes'
      }),
    /does not match declared/
  )
})

// --- Edge cases found during review of cdp.eval (returnByValue: true) ---

test('EDGE: descriptor present but dataRoot is undefined → REFUSED (fail-closed)', async () => {
  // __DEBUG_MCP_INSTANCE__ = { nonce: 'x' }  (no dataRoot)
  // expr evaluates to `undefined` (dataRoot missing)
  await assert.rejects(
    () => assertTargetAttested(fakeCdp(undefined), { expectedHome: '/tmp/sb', defaultHome: '/real' }),
    /no debug-instance descriptor/
  )
})

test('EDGE: descriptor is a string, not an object → Runtime TypeError caught → REFUSED', async () => {
  // expr: globalThis.__DEBUG_MCP_INSTANCE__.dataRoot  where instance is a string → throws
  await assert.rejects(
    () => assertTargetAttested(throwingCdp('Cannot read properties of string'), { expectedHome: '/tmp/sb', defaultHome: '/real' }),
    /no debug-instance descriptor/
  )
})

test('EDGE: cdp.eval throws (renderer disconnected / runtime error) → REFUSED', async () => {
  await assert.rejects(
    () => assertTargetAttested(throwingCdp('Target closed'), { expectedHome: '/tmp/sb', defaultHome: '/real' }),
    /no debug-instance descriptor/
  )
})

test('EDGE: descriptor returns an object {dataRoot} (not a bare string) → fallback reads .dataRoot', async () => {
  // If the eval expression ever changes to return the whole object, the
  // `d?.dataRoot ?? null` fallback must still extract it.
  const cdpObj = {
    eval: async () => ({ dataRoot: '/tmp/sb' }) // simulates returnByValue of an object
  }
  await assert.doesNotReject(
    () => assertTargetAttested(cdpObj, { expectedHome: '/tmp/sb', defaultHome: '/real' })
  )
})

test('EDGE: "~" in EXPECTED_HOME is NOT expanded by path.resolve → mismatch → REFUSED', async () => {
  // Operator passed ~/sandbox; main.ts would have resolved HERMES_HOME via
  // os.homedir() so the descriptor carries the real path. The declared home
  // with a literal "~" will not match → REFUSED (documents expected behavior).
  await assert.rejects(
    () =>
      assertTargetAttested(fakeCdp('/Users/me/sandbox'), {
        expectedHome: '~/sandbox',
        defaultHome: '/Users/me/.hermes'
      }),
    /does not match declared/
  )
})

test('EDGE: descriptor dataRoot has trailing slash — canon collapses → allowed', async () => {
  // Electron path.resolve('/tmp/sb/') === '/tmp/sb'; declared '/tmp/sb' also resolves.
  await assert.doesNotReject(
    () => assertTargetAttested(fakeCdp('/tmp/sb/'), { expectedHome: '/tmp/sb', defaultHome: '/real' })
  )
})

test('EDGE: relative EXPECTED_HOME resolved against cwd — mismatch with absolute descriptor → REFUSED', async () => {
  // Operator passed a relative path; descriptor carries absolute. They differ.
  await assert.rejects(
    () => assertTargetAttested(fakeCdp('/abs/sandbox'), { expectedHome: 'rel/sandbox', defaultHome: '/real' }),
    /does not match declared/
  )
})
test('EDGE: macOS /tmp symlink — both declared and descriptor use lexical resolve → allowed', async () => {
  // Electron main computes dataRoot via path.resolve(env.HERMES_HOME), and the
  // operator passes the same string to EXPECTED_HOME, so both sides compare
  // lexically (path.resolve). The renderer never reports a realpath form.
  await assert.doesNotReject(
    () =>
      assertTargetAttested(fakeCdp('/tmp/sb'), {
        expectedHome: '/tmp/sb',
        defaultHome: '/Users/me/.hermes'
      })
  )
})

