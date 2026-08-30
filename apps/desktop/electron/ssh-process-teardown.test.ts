import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  type SshConnectionStateLike,
  teardownSshConnectionProcessTree,
  teardownSshConnectionWithProcessTree
} from './ssh-process-teardown'

// Pins the deleted-bot python leak (#94959): when a bot/profile is torn
// down over SSH, the LOCAL ssh child process (which tunnels the remote
// dashboard's python.exe grandchildren) must be tree-killed BEFORE the SSH
// channel closes — otherwise the tunnel briefly holds the remote python
// alive after `ssh.close()` returns, and Windows surfaces a residual
// python.exe (2) in Task Manager.

// ---------------------------------------------------------------------------
// teardownSshConnectionProcessTree
// ---------------------------------------------------------------------------

test('teardownSshConnectionProcessTree tree-kills the local ssh pid', () => {
  const killed: number[] = []

  const state: SshConnectionStateLike = {
    pid: 1234,
    localPort: 4001,
    remotePort: 4002,
    ssh: { close: async () => undefined }
  }

  const killed_pid = teardownSshConnectionProcessTree(state, {
    forceKillProcessTree: pid => killed.push(pid)
  })

  assert.equal(killed_pid, 1234)
  assert.deepEqual(killed, [1234])
})

test('teardownSshConnectionProcessTree is a no-op when the state has no usable pid', () => {
  const killed: number[] = []

  for (const badState of [
    null,
    undefined,
    { pid: null, ssh: { close: async () => undefined } },
    { pid: 0, ssh: { close: async () => undefined } },
    { pid: -1, ssh: { close: async () => undefined } },
    { pid: Number.NaN, ssh: { close: async () => undefined } }
  ] as Array<SshConnectionStateLike | null | undefined>) {
    assert.equal(teardownSshConnectionProcessTree(badState, { forceKillProcessTree: pid => killed.push(pid) }), null)
  }

  assert.deepEqual(killed, [])
})

test('teardownSshConnectionProcessTree swallows forceKillProcessTree errors', () => {
  const killed: number[] = []

  const state: SshConnectionStateLike = {
    pid: 99,
    localPort: 0,
    remotePort: 0,
    ssh: { close: async () => undefined }
  }

  // The wrapper must not propagate the kill failure — channel close below
  // is the second-line cleanup, and a thrown exception here would skip it.
  assert.doesNotThrow(() =>
    teardownSshConnectionProcessTree(state, {
      forceKillProcessTree: () => {
        throw new Error('taskkill failed: process gone')
      }
    })
  )

  // Sanity: a successful kill is still recorded normally.
  assert.equal(
    teardownSshConnectionProcessTree({ ...state, pid: 88 }, { forceKillProcessTree: pid => killed.push(pid) }),
    88
  )
  assert.deepEqual(killed, [88])
})

// ---------------------------------------------------------------------------
// teardownSshConnectionWithProcessTree — the ordering pin for #94959
// ---------------------------------------------------------------------------

test('teardownSshConnectionWithProcessTree tree-kills the local ssh pid BEFORE closing the channel', async () => {
  const events: string[] = []

  const state: SshConnectionStateLike = {
    pid: 4242,
    localPort: 5001,
    remotePort: 5002,
    ssh: {
      cancelForward: async () => undefined,
      close: async () => {
        events.push('close')
      }
    }
  }

  const killed_pid = await teardownSshConnectionWithProcessTree(state, {
    forceKillProcessTree: pid => {
      events.push(`tree-kill:${pid}`)
    }
  })

  assert.equal(killed_pid, 4242)
  // The pre-fix code only called close(). Pin the ordering: tree-kill MUST
  // happen before close, otherwise a slow close() lets the tunnel outlive
  // the kill and the remote python grandchildren survive the teardown.
  assert.deepEqual(events, ['tree-kill:4242', 'close'])
})

test('teardownSshConnectionWithProcessTree cancels the forwarded port before closing the channel', async () => {
  const events: string[] = []

  const state: SshConnectionStateLike = {
    pid: 7,
    localPort: 6001,
    remotePort: 6002,
    ssh: {
      cancelForward: async (l, r) => {
        events.push(`cancel-forward:${l}:${r}`)
      },
      close: async () => {
        events.push('close')
      }
    }
  }

  const killed_pid = await teardownSshConnectionWithProcessTree(state, {
    forceKillProcessTree: () => undefined
  })

  assert.equal(killed_pid, 7)
  assert.deepEqual(events, ['cancel-forward:6001:6002', 'close'])
})

test('teardownSshConnectionWithProcessTree tolerates cancelForward + close failures', async () => {
  const killed: number[] = []

  const state: SshConnectionStateLike = {
    pid: 5,
    localPort: 1,
    remotePort: 2,
    ssh: {
      cancelForward: async () => {
        throw new Error('cancel failed')
      },
      close: async () => {
        throw new Error('close failed')
      }
    }
  }

  const killed_pid = await teardownSshConnectionWithProcessTree(state, {
    forceKillProcessTree: pid => killed.push(pid)
  })

  // The local PID was still tree-killed even though the channel cleanup
  // threw — the kill is the load-bearing guarantee for #94959.
  assert.equal(killed_pid, 5)
  assert.deepEqual(killed, [5])
})

test('teardownSshConnectionWithProcessTree on null state is a no-op', async () => {
  const killed: number[] = []

  for (const badState of [null, undefined] as Array<SshConnectionStateLike | null | undefined>) {
    const killed_pid = await teardownSshConnectionWithProcessTree(badState, {
      forceKillProcessTree: pid => killed.push(pid)
    })

    assert.equal(killed_pid, null)
  }

  assert.deepEqual(killed, [])
})
