import assert from 'node:assert/strict'
import crypto from 'node:crypto'

import { test } from 'vitest'

import {
  bootstrapParams,
  buildBootstrapCommand,
  buildRestampCommand,
  connectBatched,
  fingerprintToken,
  parseBootstrapResponse,
  REMOTE_BOOTSTRAP_PY,
  RESPONSE_MARKER,
  shouldFallBackToLegacy
} from './posix-remote-bootstrap'
import { createBootstrapCoordinator, sshConfigFingerprint } from './ssh-bootstrap-coordinator'
import { baseSshOptions, SshConnection } from './ssh-connection'

const OWNERSHIP_ID = 'a'.repeat(32)

function decodeParams(command: string) {
  const parts = command.trim().split(/\s+/)
  const encoded = parts[parts.length - 1].replace(/^'|'$/g, '')

  return JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'))
}

function remoteResponse(params: any, overrides: Record<string, unknown> = {}) {
  const nonce = String(overrides.spawnNonce ?? params.spawnNonce)

  return {
    arch: 'x86_64',
    hermesHome: '/home/remote/.hermes',
    hermesPath: '/home/remote/.local/bin/hermes',
    hermesVersion: 'Hermes Agent v0.20.4',
    logPath: `~/.hermes/desktop-ssh/${params.ownershipId}/${nonce}.log`,
    ok: true,
    os: 'Linux',
    ownershipId: params.ownershipId,
    pid: 4242,
    port: 35691,
    profile: params.profile,
    reused: false,
    spawnNonce: nonce,
    timings: { totalMs: 1200 },
    tokenFingerprint: params.tokenFingerprint,
    ...overrides
  }
}

function line(payload: unknown) {
  return `${RESPONSE_MARKER} ${JSON.stringify(payload)}\n`
}

/**
 * A scripted stand-in for SshConnection. Every ssh process the real class would
 * spawn shows up in `calls`, so a test can assert the invocation BUDGET, not
 * just the outcome.
 */
function fakeSsh(respond: (command: string, options: any) => Promise<string> | string) {
  const calls: { command?: string; kind: string; options?: any }[] = []

  return {
    calls,
    async cancelForward(localPort: number, remotePort: number) {
      calls.push({ kind: 'cancelForward', options: { localPort, remotePort } })
    },
    async exec(command: string, options: any = {}) {
      calls.push({ command, kind: 'exec', options })

      return respond(command, options)
    },
    async forward(localPort: number, remotePort: number, remoteHost: string, options: any = {}) {
      calls.push({ kind: 'forward', options: { localPort, remoteHost, remotePort, ...options } })
    },
    get invocations() {
      return calls.filter(call => call.kind === 'exec' || call.kind === 'forward').length
    }
  }
}

function connectDeps(ssh: any, overrides: Record<string, unknown> = {}) {
  return {
    adoptServedToken: async (_baseUrl: string, token: string) => token,
    cancelForward: (localPort: number, remotePort: number) => ssh.cancelForward(localPort, remotePort),
    forward: (localPort: number, remotePort: number, options: any) =>
      ssh.forward(localPort, remotePort, '127.0.0.1', options),
    ownershipId: OWNERSHIP_ID,
    pickLocalPort: async () => 51234,
    probeReuseProof: async () => 'authenticated-ok',
    profile: '',
    rememberLog: () => {},
    remoteHermesPath: '',
    reuseToken: '',
    ssh,
    waitForHermes: async () => ({ ok: true }),
    ...overrides
  } as any
}

// --- the remote program itself ---

test('the remote program keeps its regexes and NUL split intact through the TS literal', () => {
  // String.raw is load-bearing: a normal template literal would eat the
  // backslashes and ship a program that matches "d+" and splits on "0".
  assert.match(REMOTE_BOOTSTRAP_PY, /READY port=\(\\d\+\)/)
  assert.match(REMOTE_BOOTSTRAP_PY, /raw\.split\(b"\\0"\)/)
  assert.match(REMOTE_BOOTSTRAP_PY, /\^\[0-9a-f\]\{32\}\$/)
  // argv[1] is the program the loader exec'd; argv[2] is the parameter block.
  // Reading argv[1] for the parameters made every start fail with a JSON error
  // on the program's own source, and no mocked-ssh test could see it.
  assert.match(REMOTE_BOOTSTRAP_PY, /json\.loads\(base64\.b64decode\(sys\.argv\[2\]\)/)
  assert.match(
    buildBootstrapCommand(bootstrapParams({ ownershipId: OWNERSHIP_ID } as any, false).params),
    /sys\.argv\[1\]/
  )
  // The backend must stay loopback-only on the remote.
  assert.match(REMOTE_BOOTSTRAP_PY, /"--host",\s*\n?\s*"127\.0\.0\.1"/)
  assert.doesNotMatch(REMOTE_BOOTSTRAP_PY, /0\.0\.0\.0/)
})

// --- 6. the token is never an argument and never logged ---

test('the session token travels on stdin only, never in argv or the log', async () => {
  const logs: string[] = []
  let seenStdin = ''
  let seenCommand = ''

  const ssh = fakeSsh((command, options) => {
    seenCommand = command
    seenStdin = options.stdinData
    const params = decodeParams(command)

    return line(remoteResponse(params))
  })

  const result: any = await connectBatched(connectDeps(ssh, { rememberLog: (m: string) => logs.push(m) }))

  assert.equal(result.token, seenStdin)
  assert.ok(seenStdin.length >= 64)
  assert.ok(!seenCommand.includes(seenStdin), 'token must not appear in the remote command')

  const decoded = Buffer.from(seenCommand.trim().split(/\s+/).slice(-1)[0].replace(/'/g, ''), 'base64').toString('utf8')

  assert.ok(!decoded.includes(seenStdin), 'token must not appear in the encoded parameters')
  assert.equal(decoded.includes(fingerprintToken(seenStdin)), true, 'only the fingerprint is sent')

  for (const message of logs) {
    assert.ok(!message.includes(seenStdin), `token leaked into a log line: ${message}`)
  }

  assert.ok(!buildRestampCommand(OWNERSHIP_ID, 'b'.repeat(16), fingerprintToken(seenStdin)).includes(seenStdin))
})

// --- 1. bounded ssh invocations ---

test('a cold start costs exactly one exec and one tunnel', async () => {
  const ssh = fakeSsh(command => line(remoteResponse(decodeParams(command))))
  const result: any = await connectBatched(connectDeps(ssh))

  assert.equal(result.reused, false)
  assert.equal(ssh.invocations, 2)
  assert.deepEqual(
    ssh.calls.map(call => call.kind),
    ['exec', 'forward']
  )
})

// --- 3. a valid lock reuses the backend ---

test('a reusable lock costs the same two invocations and spawns nothing', async () => {
  const reuseToken = 'r'.repeat(64)
  let sentParams: any

  const ssh = fakeSsh(command => {
    sentParams = decodeParams(command)

    return line(
      remoteResponse(sentParams, {
        pid: 1556269,
        reused: true,
        spawnNonce: 'c'.repeat(16),
        tokenFingerprint: fingerprintToken(reuseToken)
      })
    )
  })

  const result: any = await connectBatched(connectDeps(ssh, { reuseToken }))

  assert.equal(sentParams.allowReuse, true)
  assert.equal(sentParams.reuseFingerprint, fingerprintToken(reuseToken))
  assert.equal(result.reused, true)
  assert.equal(result.pid, 1556269)
  assert.equal(result.token, reuseToken)
  assert.equal(ssh.invocations, 2)
})

test('no saved token means the remote is never allowed to reuse a lock', async () => {
  let sentParams: any

  const ssh = fakeSsh(command => {
    sentParams = decodeParams(command)

    return line(remoteResponse(sentParams))
  })

  await connectBatched(connectDeps(ssh, { reuseToken: '' }))
  assert.equal(sentParams.allowReuse, false)
  assert.equal(sentParams.reuseFingerprint, '')
})

// --- 4. a stale or unowned lock never kills a foreign process ---

test('a stale reuse proof respawns once and never trusts the first answer', async () => {
  const reuseToken = 'r'.repeat(64)
  const commands: any[] = []
  const proofs = ['authenticated-stale', 'authenticated-ok']

  const ssh = fakeSsh(command => {
    const params = decodeParams(command)
    commands.push(params)

    return line(
      params.allowReuse
        ? remoteResponse(params, {
            reused: true,
            spawnNonce: 'c'.repeat(16),
            tokenFingerprint: fingerprintToken(reuseToken)
          })
        : remoteResponse(params)
    )
  })

  const result: any = await connectBatched(
    connectDeps(ssh, { probeReuseProof: async () => proofs.shift(), reuseToken })
  )

  assert.equal(commands.length, 2)
  assert.equal(commands[0].allowReuse, true)
  // The retry forbids reuse, so the remote script takes its cleanup-then-spawn
  // branch instead of adopting a backend the dashboard just disowned.
  assert.equal(commands[1].allowReuse, false)
  assert.equal(commands[1].reuseFingerprint, '')
  assert.equal(result.reused, false)
  // The tunnel opened for the stale backend is closed before the retry.
  assert.equal(ssh.calls.filter(call => call.kind === 'cancelForward').length, 1)
})

test('a retry that still claims reuse is rejected instead of trusted', async () => {
  // The retry forbids reuse. A remote that answers `reused: true` anyway is
  // not describing a backend we asked for, so it is refused rather than
  // adopted - and refused BEFORE any credential is handed to it.
  const ssh = fakeSsh(command =>
    line(
      remoteResponse(decodeParams(command), {
        reused: true,
        spawnNonce: 'c'.repeat(16),
        tokenFingerprint: fingerprintToken('r'.repeat(64))
      })
    )
  )

  await assert.rejects(
    connectBatched(
      connectDeps(ssh, { probeReuseProof: async () => 'authenticated-stale', reuseToken: 'r'.repeat(64) })
    ),
    /unusable response \(reuse fingerprint mismatch\)/
  )
  assert.equal(ssh.calls.filter(call => call.kind === 'cancelForward').length, 1)
})

test('the remote program only terminates a pid whose argv proves it is ours', () => {
  // The ownership proof is the guard that keeps a foreign pid alive: cleanup
  // is gated on `owned`, which is gated on the argv check.
  assert.match(
    REMOTE_BOOTSTRAP_PY,
    /def cleanup_stale\(oid, lock, alive, owned\):\n {4}if lock and alive and owned:\n {8}terminate\(lock\["pid"\]\)/
  )
  assert.match(REMOTE_BOOTSTRAP_PY, /owned = alive and is_our_backend\(/)
  assert.match(REMOTE_BOOTSTRAP_PY, /args\[owner \+ 1\] == nonce/)
})

// --- 2. strict validation of the single JSON response ---

test('the bootstrap response is validated field by field', () => {
  const { params } = bootstrapParams({ ownershipId: OWNERSHIP_ID } as any, false)
  const good = remoteResponse(params)

  assert.equal(parseBootstrapResponse(line(good), params).port, 35691)

  const rejected: [string, unknown][] = [
    ['port 0', { ...good, port: 0 }],
    ['port out of range', { ...good, port: 70000 }],
    ['pid 0', { ...good, pid: 0 }],
    ['other ownership', { ...good, ownershipId: 'b'.repeat(32) }],
    ['other profile', { ...good, profile: 'someone-else' }],
    ['bad nonce', { ...good, spawnNonce: 'zz' }],
    ['bad fingerprint', { ...good, tokenFingerprint: 'nope' }],
    ['missing reuse flag', { ...good, reused: 'yes' }],
    ['foreign spawn nonce', { ...good, spawnNonce: 'd'.repeat(16) }],
    ['foreign token fingerprint', { ...good, tokenFingerprint: 'e'.repeat(32) }],
    ['log path escape', { ...good, logPath: '~/.hermes/desktop-ssh/other/x.log' }]
  ]

  for (const [label, payload] of rejected) {
    assert.throws(() => parseBootstrapResponse(line(payload), params), /unusable response/, label)
  }

  assert.throws(() => parseBootstrapResponse('', params), /no response line/)
  assert.throws(() => parseBootstrapResponse(`${RESPONSE_MARKER} not-json`, params), /not JSON/)
  // Remote chatter before the response line must not break parsing.
  assert.equal(parseBootstrapResponse(`motd\nwarning: x\n${line(good)}`, params).pid, 4242)
})

test('a reported remote failure keeps its kind and never falls back', () => {
  const { params } = bootstrapParams({ ownershipId: OWNERSHIP_ID } as any, false)

  const error: any = (() => {
    try {
      parseBootstrapResponse(line({ error: 'Hermes is not installed', kind: 'hermes-not-found', ok: false }), params)
    } catch (caught) {
      return caught
    }
  })()

  assert.equal(error.kind, 'hermes-not-found')
  assert.equal(shouldFallBackToLegacy(error), false)
  // An unlisted kind is not trusted verbatim.
  assert.throws(
    () => parseBootstrapResponse(line({ error: 'x', kind: 'made-up', ok: false }), params),
    (thrown: any) => thrown.kind === 'unknown'
  )
})

test('a transport failure is surfaced, a missing python3 falls back', () => {
  for (const kind of ['auth-failed', 'host-key-changed', 'superseded', 'timeout', 'unreachable']) {
    assert.equal(shouldFallBackToLegacy(Object.assign(new Error('x'), { kind })), false)
  }

  assert.equal(shouldFallBackToLegacy(new Error('python3: command not found')), true)
  assert.equal(shouldFallBackToLegacy(Object.assign(new Error('x'), { kind: 'batched-unavailable' })), true)
})

// --- 5. a backend that never announces its port ---

test('a ready timeout arrives as an actionable error, not a hang', async () => {
  const ssh = fakeSsh(() =>
    line({
      error: 'Timed out waiting for the remote dashboard to announce its port (45000ms).',
      kind: 'ready-timeout',
      ok: false
    })
  )

  await assert.rejects(connectBatched(connectDeps(ssh)), (error: any) => {
    assert.equal(error.kind, 'ready-timeout')
    assert.match(error.message, /Timed out waiting for the remote dashboard/)

    return true
  })

  // The exec is bounded by the remote readiness budget plus a margin, so a
  // wedged remote cannot pin the ssh child forever.
  const params = bootstrapParams({ ownershipId: OWNERSHIP_ID, readyTimeoutMs: 45_000 } as any, false).params
  assert.equal(params.readyTimeoutMs, 45_000)
  assert.ok(ssh.calls[0].options.timeoutMs > params.readyTimeoutMs)
  // Nothing was forwarded, so nothing leaks.
  assert.equal(ssh.calls.filter(call => call.kind === 'forward').length, 0)
})

// --- 7. the tunnel is closed when HTTP or WebSocket fails ---

test('an HTTP failure closes the tunnel', async () => {
  const ssh = fakeSsh(command => line(remoteResponse(decodeParams(command))))

  await assert.rejects(
    connectBatched(
      connectDeps(ssh, {
        waitForHermes: async () => {
          throw new Error('backend never answered /api/status')
        }
      })
    ),
    /never answered/
  )

  assert.equal(ssh.calls.filter(call => call.kind === 'cancelForward').length, 1)
})

test('a WebSocket rejection closes the tunnel and reports the reason', async () => {
  const ssh = fakeSsh(command => line(remoteResponse(decodeParams(command))))

  await assert.rejects(
    connectBatched(connectDeps(ssh, { probeWebSocket: async () => ({ ok: false, reason: '401 unauthorized' }) })),
    /WebSocket \(\/api\/ws\) rejected the session token: 401 unauthorized/
  )

  assert.equal(ssh.calls.filter(call => call.kind === 'cancelForward').length, 1)
})

test('a backend serving a foreign ownership nonce is refused', async () => {
  const ssh = fakeSsh(command => line(remoteResponse(decodeParams(command))))

  await assert.rejects(
    connectBatched(connectDeps(ssh, { probeReuseProof: async () => 'authenticated-stale' })),
    (error: any) => {
      assert.equal(error.kind, 'foreign-backend')

      return true
    }
  )

  assert.equal(ssh.calls.filter(call => call.kind === 'cancelForward').length, 1)
})

test('served-token drift costs exactly one extra exec to re-stamp the lockfile', async () => {
  const served = 's'.repeat(64)

  const ssh = fakeSsh(command => {
    if (command.includes('restamp')) {
      return 'OK'
    }

    return line(remoteResponse(decodeParams(command)))
  })

  const result: any = await connectBatched(connectDeps(ssh, { adoptServedToken: async () => served }))

  assert.equal(result.token, served)
  assert.equal(result.tokenFingerprint, fingerprintToken(served))
  assert.equal(ssh.invocations, 3)
})

// --- 9. cancellation leaves nothing behind ---

test('a cancelled bootstrap aborts the ssh child and opens no tunnel', async () => {
  const controller = new AbortController()

  const ssh = fakeSsh((_command, options) => {
    assert.equal(options.signal, controller.signal, 'the abort signal must reach the ssh child')
    controller.abort()
    const error: any = new Error('SSH operation was cancelled.')
    error.kind = 'superseded'

    throw error
  })

  await assert.rejects(connectBatched(connectDeps(ssh, { signal: controller.signal })), (error: any) => {
    assert.equal(error.kind, 'superseded')

    return true
  })

  assert.equal(ssh.calls.filter(call => call.kind === 'forward').length, 0)
  assert.equal(shouldFallBackToLegacy({ kind: 'superseded' }), false)
})

test('an abort raised after the tunnel is up still closes the tunnel', async () => {
  const controller = new AbortController()
  const ssh = fakeSsh(command => line(remoteResponse(decodeParams(command))))

  await assert.rejects(
    connectBatched(
      connectDeps(ssh, {
        signal: controller.signal,
        waitForHermes: async () => {
          controller.abort()
        }
      })
    ),
    (error: any) => {
      assert.equal(error.kind, 'superseded')

      return true
    }
  )

  assert.equal(ssh.calls.filter(call => call.kind === 'cancelForward').length, 1)
})

// --- 8. concurrent callers join one bootstrap ---

test('two callers for the same connection and profile join one bootstrap', async () => {
  const coordinator = createBootstrapCoordinator()
  const scope = 'conn:alfred-laptop::default'
  const config = { host: 'box.test', port: 22, remoteProfile: '', user: 'hermes' }
  const fingerprint = sshConfigFingerprint(scope, config)
  let runs = 0

  const start = () =>
    coordinator.start(scope, fingerprint, async () => {
      runs += 1
      await new Promise(resolve => setTimeout(resolve, 5))

      return 'connection'
    })

  const [a, b] = await Promise.all([start(), start()])

  assert.equal(runs, 1)
  assert.equal(a, b)
})

// --- 10. POSIX clients keep ControlMaster ---

test('POSIX keeps one multiplexed connection and ignores the lazy shortcut', async () => {
  const spawned: string[][] = []

  // `-O check` fails: no master is up yet, so open() must establish one.
  const spawnFn = (_cmd: string, args: string[]) => {
    spawned.push(args)
    const code = args.includes('check') ? 255 : 0

    return {
      on: (event: string, handler: any) => {
        if (event === 'close') {
          setTimeout(() => handler(code), 0)
        }
      },
      stderr: { on: () => {} },
      stdout: { on: () => {} }
    } as any
  }

  const conn: any = new SshConnection(
    { host: 'box.test', user: 'hermes' },
    { controlDir: '/tmp/hermes-test-sockets', mux: true, spawnFn }
  )

  assert.ok(conn.controlPath, 'a multiplexed connection owns a control socket')
  assert.deepEqual(baseSshOptions(conn.controlPath).slice(0, 6), [
    '-o',
    `ControlPath=${conn.controlPath}`,
    '-o',
    'ControlMaster=auto',
    '-o',
    'ControlPersist=300'
  ])

  // `lazy` is a no-mux-only shortcut: on POSIX the master is still opened, so
  // exec/forward keep sharing that single authentication.
  await conn.open({ lazy: true })
  assert.ok(spawned.length > 0, 'the master handshake still runs')
  assert.ok(
    spawned.some(args => args.includes('-M')),
    'ControlMaster is still established'
  )

  const noMux: any = new SshConnection({ host: 'box.test', user: 'hermes' }, { mux: false, spawnFn })
  const before = noMux.invocations
  await noMux.open({ lazy: true })

  assert.equal(noMux.controlPath, '')
  assert.equal(noMux.invocations - before, 0, 'the lazy no-mux open costs no authentication')
})

test('the batched command carries no secret and stays a single python3 invocation', () => {
  const { params, token } = bootstrapParams(
    { ownershipId: OWNERSHIP_ID, profile: 'writer', remoteHermesPath: '/srv/hermes' } as any,
    true,
    () => crypto.randomBytes(32).toString('hex')
  )

  const command = buildBootstrapCommand(params)

  assert.equal(command.startsWith('python3 -c '), true)
  assert.equal(command.split('python3').length, 2, 'exactly one remote interpreter invocation')
  assert.ok(!command.includes(token))
  assert.deepEqual(decodeParams(command), params)
})
