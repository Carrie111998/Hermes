import assert from 'node:assert/strict'

import { test } from 'vitest'

import { collectLiveStragglerPids, isLiveProcessRoot, stopBackendChild, stopBackendTreesForUpdate } from './backend-child'

// A reaped child must never be tree-killed by PID (#89614).
//
// `child.pid` is not a liveness check: Node keeps the number after the child
// is reaped, and `child.killed` stays FALSE for a child that exited on its own
// rather than being signalled. Every guard here used to be
// `Number.isInteger(child.pid)`, which passes for a dead child.
//
// On Windows that is not a harmless no-op. PIDs are recycled aggressively, so
// `taskkill /PID <recycled> /T /F` kills whoever inherited the number; when
// that is a protected system process the kernel bugchecks 0xEF
// (CRITICAL_PROCESS_DIED) and the machine blue-screens, which WER attributes
// to taskkill.exe.
//
// These tests pin the liveness contract rather than any one call site.

const live = (pid: number) => ({ exitCode: null, pid, signalCode: null })
const exited = (pid: number, code = 0) => ({ exitCode: code, pid, signalCode: null })
const signalled = (pid: number, sig = 'SIGTERM') => ({ exitCode: null, pid, signalCode: sig })

test('isLiveProcessRoot rejects a reaped child that still carries its pid', () => {
  assert.equal(isLiveProcessRoot(live(4242)), true)
  // THE regression: exitCode 0 with a populated pid is the shape Node leaves
  // behind, and it used to pass every `Number.isInteger(pid)` guard.
  assert.equal(isLiveProcessRoot(exited(4242)), false)
  assert.equal(isLiveProcessRoot(signalled(4242)), false)
})

test('isLiveProcessRoot rejects absent and nonsensical pids', () => {
  assert.equal(isLiveProcessRoot(null), false)
  assert.equal(isLiveProcessRoot(undefined), false)
  assert.equal(isLiveProcessRoot({ exitCode: null, pid: null, signalCode: null }), false)
  assert.equal(isLiveProcessRoot({ exitCode: null, pid: 0, signalCode: null }), false)
  // A negative pid would be a POSIX process-GROUP id. taskkill has no such
  // notion, so letting one through would pass a nonsense argument to it.
  assert.equal(isLiveProcessRoot({ exitCode: null, pid: -991, signalCode: null }), false)
})

// ---------------------------------------------------------------------------
// Stale-record witness: a bare `{ pid }` carries no evidence of authority, so
// it must not reach any pid-based mutator. Liveness is witnessed, not assumed.
// ---------------------------------------------------------------------------

test('a pid-only record is not live and cannot enter the force path', () => {
  const stale = { pid: 77 }

  assert.equal(isLiveProcessRoot(stale), false, 'no lifecycle fields means no proof of liveness')

  // The predicate is only useful if the callers that mutate by pid honour it.
  assert.deepEqual(collectLiveStragglerPids(stale, [{ pid: 78 }, { pid: 79 }]), [], 'no stale pid may be swept')

  const treeKilled: number[] = []

  stopBackendTreesForUpdate(stale, {
    forceKillProcessTree: pid => treeKilled.push(pid),
    stopAllPoolBackends: () => {}
  })

  assert.deepEqual(treeKilled, [], 'a record with no lifecycle evidence must never be tree-killed')
})

test('a half-populated record is not live either', () => {
  // Exactly one field present is still not a witness -- an object that carries
  // exitCode but no signalCode cannot rule out having been signalled.
  assert.equal(isLiveProcessRoot({ exitCode: null, pid: 77 }), false)
  assert.equal(isLiveProcessRoot({ pid: 77, signalCode: null }), false)
})

test('collectLiveStragglerPids drops reaped roots and keeps live ones', () => {
  const pids = collectLiveStragglerPids(exited(100), [live(200), exited(300), signalled(400), live(500)])

  assert.deepEqual(pids, [200, 500])
})

test('collectLiveStragglerPids tolerates an empty or absent pool', () => {
  assert.deepEqual(collectLiveStragglerPids(live(11), []), [11])
  assert.deepEqual(collectLiveStragglerPids(null, [null, undefined]), [])
})

test('collectLiveStragglerPids stops re-killing a root once it is reaped', () => {
  // The update hand-off re-collects on EVERY pass of its wait loop. Pass 1
  // kills the child; by pass 2 the handle is reaped and its number may already
  // belong to something else, so it must drop out of the sweep.
  const root = { exitCode: null as null | number, pid: 6060, signalCode: null }

  assert.deepEqual(collectLiveStragglerPids(root, []), [6060], 'pass 1 kills the live root')

  root.exitCode = 0

  assert.deepEqual(collectLiveStragglerPids(root, []), [], 'pass 2 must not re-kill the recycled number')
})

// ---------------------------------------------------------------------------
// stopBackendChild mutates ONLY through the retained handle. There is no
// dependency injection left to spy on, because there is no pid-based mutator
// left to inject -- so these watch process.kill itself, which is the only way
// one could come back.
// ---------------------------------------------------------------------------

function withKillSpy(body: () => void): Array<[number, unknown]> {
  const pidKills: Array<[number, unknown]> = []
  const realKill = process.kill

  process.kill = ((pid: number, signal?: unknown) => {
    pidKills.push([pid, signal])
  }) as typeof process.kill

  try {
    body()
  } finally {
    process.kill = realKill
  }

  return pidKills
}

test('stopBackendChild issues no pid-based kill for a child that exited on its own', () => {
  let handleKills = 0
  const child = { ...exited(1234), kill: () => (handleKills += 1), killed: false }

  const pidKills = withKillSpy(() => stopBackendChild(child))

  // `killed: false` is the crux: this child was never signalled by us, so the
  // pre-existing `child.killed` guard does not catch it. Before #89614 it took
  // a taskkill against a number Windows may already have reassigned.
  assert.deepEqual(pidKills, [])
  // The HANDLE kill still runs, and is safe: on a reaped child Node has already
  // dropped the internal handle, so `child.kill()` returns false without
  // issuing any syscall against the number.
  assert.equal(handleKills, 1)
})

test('stopBackendChild issues no pid-based kill for a LIVE child either', () => {
  // The contract is not "be careful with dead children", it is "never mutate
  // by number". A live child is the case where the old code was most confident
  // it was allowed to tree-kill, and it is still not allowed to.
  const calls: string[] = []
  const child = { ...live(1234), kill: (s: string) => calls.push(s), killed: false }

  const pidKills = withKillSpy(() => stopBackendChild(child))

  assert.deepEqual(pidKills, [])
  assert.deepEqual(calls, ['SIGTERM'])
})

// ---------------------------------------------------------------------------
// PID-reuse witness: once the owned child exits, its number may belong to an
// unrelated process. Nothing we do afterwards may touch that number.
// ---------------------------------------------------------------------------

test('a recycled pid belonging to an unrelated process is never mutated', () => {
  const RECYCLED = 4242

  // The child we owned. It exited on its own, so `killed` is false and `pid`
  // is still populated -- the exact shape that used to pass every guard.
  let ownedHandleKills = 0
  const owned = { ...exited(RECYCLED), kill: () => (ownedHandleKills += 1), killed: false }

  // Somebody else now holds that number. Its only mutator is its own handle,
  // which we do not have, so any kill that reaches it must come through the pid.
  let strangerKills = 0
  const stranger = { ...live(RECYCLED), kill: () => (strangerKills += 1), killed: false }

  assert.equal(stranger.pid, owned.pid, 'the premise: both refer to the same number')

  const treeKilled: number[] = []

  const pidKills = withKillSpy(() => {
    stopBackendChild(owned)

    // Every pid-based path in this module, given the reaped owner.
    assert.deepEqual(collectLiveStragglerPids(owned, [owned]), [], 'sweep must not surface the recycled number')

    stopBackendTreesForUpdate(owned, {
      forceKillProcessTree: pid => treeKilled.push(pid),
      stopAllPoolBackends: () => {}
    })
  })

  assert.deepEqual(treeKilled, [], `taskkill must never be issued against ${RECYCLED}`)
  assert.deepEqual(pidKills, [], `no process.kill may be issued against ${RECYCLED}`)
  assert.equal(strangerKills, 0, 'the unrelated process holding the recycled pid must be untouched')
  // The owner's own handle is still signalled -- that is the safe path, and it
  // cannot reach the stranger because the handle, not the number, is the target.
  assert.equal(ownedHandleKills, 1)
})

test('stopBackendTreesForUpdate skips a reaped primary but still drains the pool', () => {
  const killed: number[] = []
  let pooled = 0

  stopBackendTreesForUpdate(exited(555), {
    forceKillProcessTree: pid => killed.push(pid),
    stopAllPoolBackends: () => {
      pooled += 1
    }
  })

  assert.deepEqual(killed, [], 'the reaped primary must not be tree-killed')
  // Pool teardown is unconditional: skipping the dead primary must not skip
  // the live grandchildren the hand-off is actually trying to clear.
  assert.equal(pooled, 1)
})

test('stopBackendTreesForUpdate still tree-kills a live primary first', () => {
  const order: string[] = []

  stopBackendTreesForUpdate(live(555), {
    forceKillProcessTree: pid => order.push(`kill:${pid}`),
    stopAllPoolBackends: () => order.push('pool')
  })

  // Ordering matters: if the primary root exits before taskkill /T enumerates
  // it, Windows can no longer reach its grandchildren.
  assert.deepEqual(order, ['kill:555', 'pool'])
})
