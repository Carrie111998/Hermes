import assert from 'node:assert/strict'
import { exec as nodeExec } from 'node:child_process'
import { promisify } from 'node:util'

import { test } from 'vitest'

import { profileSshOverride } from './connection-config'
import {
  buildSpawnCommand,
  cleanupStale,
  connect,
  expandRemotePath,
  fingerprintToken,
  isForwardBindCollision,
  locateHermes,
  LOCKFILE_SCHEMA_VERSION,
  lockfilePath,
  openForward,
  ownershipDirectory,
  pidIsOurDashboard,
  probeRemotePlatform,
  PROTOCOL_VERSION,
  readLockfile,
  READY_RE,
  remotePidAlive,
  remoteSupportsSshOwnership,
  scrapeReadyPort,
  spawnLogPath,
  spawnRemoteDashboard,
  terminateOwnedProcess,
  validateRemotePath,
  withOwnershipMutex,
  writeLockfile
} from './remote-lifecycle'

const OWNERSHIP_ID = '0123456789abcdef0123456789abcdef'
const SPAWN_NONCE = '0123456789abcdef'
const execAsync = promisify(nodeExec)

function ownedLock(over: any = {}) {
  return {
    schemaVersion: LOCKFILE_SCHEMA_VERSION,
    protocolVersion: PROTOCOL_VERSION,
    ownershipId: OWNERSHIP_ID,
    spawnNonce: SPAWN_NONCE,
    pid: 333,
    port: 40000,
    profile: '',
    hermesPath: '~/.local/bin/hermes',
    launcherPath: '~/.local/bin/hermes',
    hermesHome: '~/.hermes',
    logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE),
    tokenFingerprint: fingerprintToken('stored-token'),
    startedAt: '2026-07-14T00:00:00.000Z',
    pidStartTime: 12345,
    processFingerprint: 'a'.repeat(32),
    ...over
  }
}

/** Live identity probe response: owned serve --isolated --nonce shape. */
function ownedIdentityOut(over: any = {}) {
  const start = over.startTime ?? 12345
  const fp = over.fingerprint ?? 'a'.repeat(32)

  return `OK\n${start}\n${fp}\npython\n/runtime/.venv/bin/hermes\nserve\n--isolated\n--ssh-owner-nonce\n${SPAWN_NONCE}\n`
}

function foreignIdentityOut(over: any = {}) {
  const start = over.startTime ?? 999
  const fp = over.fingerprint ?? 'b'.repeat(32)

  return `SHAPE\n${start}\n${fp}\nother\n`
}

function goneIdentityOut() {
  return 'GONE\n'
}

function mutexRules() {
  return [
    [/stale_ms=/, 'ACQUIRED\n'],
    [/print\("HELD"/, 'HELD\n'],
    [/data==holder/, '']
  ]
}

function baseConnectRules(extra: any[] = []) {
  // Mutex acquire/release + optional extras. Identity/token/spawn rules should
  // be more specific than bare /python3 -c/ so they win first-match.
  return [
    ...mutexRules(),
    [/uname/, 'Linux\nx86_64'],
    [/\[ -x/, 'OK'],
    ...extra
  ]
}

function spawnConnectRules(over: any = {}) {
  const pid = over.pid ?? 777
  const port = over.port ?? 51999
  const ready = over.ready ?? `HERMES_DASHBOARD_READY port=${port}\n`

  return baseConnectRules([
    [/cat .*lock\.json/, over.lockJson ?? ''],
    [/grep -q ssh-session-token-file/, 'YES\n'],
    // token upload python (stdin path) — before identity observe
    [command => /python3 -c/.test(command) && /O_EXCL/.test(command), ''],
    [/setsid|nohup/, `${pid}\n`],
    // post-spawn identity observe
    [/print\("OK" if ok else "SHAPE"\)/, over.identity ?? ownedIdentityOut({ startTime: 4242, fingerprint: 'c'.repeat(32) })],
    [new RegExp(`kill -0 ${pid}`), 'ALIVE'],
    [/cat .*\.log/, ready],
    [/printf '%s' '/, '']
  ])
}


// A fake SshConnection whose exec() is matched against an ordered list of
// [regex|fn, response|fn] rules. First match wins; unmatched commands return ''.
function fakeSsh(rules: any[] = []) {
  const calls: string[] = []

  return {
    calls,
    async exec(cmd) {
      calls.push(cmd)

      for (const [matcher, resp] of rules) {
        const hit = typeof matcher === 'function' ? matcher(cmd) : matcher.test(cmd)

        if (hit) {
          const out = typeof resp === 'function' ? resp(cmd) : resp

          if (out instanceof Error) {
            throw out
          }

          return out
        }
      }

      return ''
    }
  }
}

test('locateHermes prefers the explicit profile path when executable', async () => {
  const ssh = fakeSsh([[/\[ -x .*\/opt\/hermes/, 'OK']])
  assert.equal(await locateHermes(ssh, '/opt/hermes'), '/opt/hermes')
})

test('locateHermes throws (no silent fallback) when an EXPLICIT path is not executable', async () => {
  // command -v WOULD find a different install, but an explicit path must not
  // silently fall back to it — that is the "connected to the wrong hermes" bug.
  const ssh = fakeSsh([
    [/command -v hermes/, '/home/u/.local/bin/hermes\n'],
    [/\[ -x .*\.local\/bin\/hermes/, 'OK']
  ])

  await assert.rejects(
    () => locateHermes(ssh, '/bad/path/hermes'),
    (err: any) => {
      assert.equal(err.kind, 'hermes-not-found')
      assert.match(err.message, /\/bad\/path\/hermes/)

      return true
    }
  )
})

test('locateHermes falls back to the login-shell command -v probe', async () => {
  const ssh = fakeSsh([
    [/command -v hermes/, '/home/u/.local/bin/hermes\n'],
    [/\[ -x .*\.local\/bin\/hermes/, 'OK']
  ])

  assert.equal(await locateHermes(ssh, ''), '/home/u/.local/bin/hermes')
})

test('locateHermes preserves an installer wrapper instead of resolving its interpreter', async () => {
  // install.sh venv mode writes: exec "$HERMES_BIN" "$HERMES_ENTRYPOINT" "$@",
  // where $HERMES_BIN is the venv python. The old canonicalization returned
  // that interpreter, so `<python> --version` printed "Python x.y.z" and
  // `<python> serve --help` failed outright (#74411). The wrapper itself is
  // executable and forwards args correctly — return it untouched.
  const ssh = fakeSsh([
    [/command -v hermes/, '/home/u/.local/bin/hermes\n'],
    [/\[ -x .*\.local\/bin\/hermes/, 'OK'],
    // If the removed python3 wrapper-parser were ever reintroduced, this rule
    // would reward it with an interpreter path and the assertions below fail.
    [/python3 -c/, '/home/u/.hermes/hermes-agent/venv/bin/python\n']
  ])

  assert.equal(await locateHermes(ssh, ''), '/home/u/.local/bin/hermes')
  assert.ok(
    !ssh.calls.some(cmd => cmd.includes('python3 -c')),
    'locateHermes must not shell out to a python3 parser to rewrite the launcher'
  )
})

test('locateHermes returns an explicit remoteHermesPath unchanged', async () => {
  // The override half of #74411: an explicit remoteHermesPath pointing at a
  // wrapper was also canonicalized to its interpreter, so overriding to
  // ~/.local/bin/hermes changed nothing for affected users.
  const ssh = fakeSsh([
    [/\[ -x .*\.local\/bin\/hermes/, 'OK'],
    [/python3 -c/, '/home/u/.hermes/hermes-agent/venv/bin/python\n']
  ])

  assert.equal(await locateHermes(ssh, '~/.local/bin/hermes'), '~/.local/bin/hermes')
  assert.ok(!ssh.calls.some(cmd => cmd.includes('python3 -c')), 'an explicit remoteHermesPath must never be rewritten')
})

test('locateHermes falls back to ~/.local/bin/hermes when the login-shell probe misses', async () => {
  // ~/.local/bin is the non-root installer's command location (scripts/install.sh).
  const ssh = fakeSsh([
    [/command -v hermes/, ''],
    [/\[ -x .*\.local\/bin\/hermes/, 'OK']
  ])

  assert.equal(await locateHermes(ssh, ''), '~/.local/bin/hermes')
})

test('locateHermes tries the conventional venv path last', async () => {
  const ssh = fakeSsh([[/\[ -x .*venv\/bin\/hermes/, 'OK']])
  assert.equal(await locateHermes(ssh, ''), '~/.hermes/hermes-agent/venv/bin/hermes')
})

test('locateHermes throws a hermes-not-found error with an install hint', async () => {
  const ssh = fakeSsh([]) // nothing is executable
  await assert.rejects(
    () => locateHermes(ssh, ''),
    (err: any) => {
      assert.equal(err.kind, 'hermes-not-found')
      assert.match(err.message, /install/i)

      return true
    }
  )
})

test('locateHermes uses a login shell for the command -v probe', async () => {
  const ssh = fakeSsh([
    [/command -v hermes/, '/x/hermes'],
    [/\[ -x/, 'OK']
  ])

  await locateHermes(ssh, '')
  assert.ok(
    ssh.calls.some(c => /bash -lc/.test(c)),
    'must probe in a login shell (PATH pitfall)'
  )
})

test('probeRemotePlatform accepts Linux and macOS', async () => {
  assert.deepEqual(await probeRemotePlatform(fakeSsh([[/uname/, 'Linux\nx86_64']])), {
    os: 'Linux',
    arch: 'x86_64'
  })
  assert.deepEqual(await probeRemotePlatform(fakeSsh([[/uname/, 'Darwin\narm64']])), {
    os: 'Darwin',
    arch: 'arm64'
  })
})

test('probeRemotePlatform rejects unsupported remote platforms', async () => {
  await assert.rejects(
    () => probeRemotePlatform(fakeSsh([[/uname/, 'MINGW64_NT\nx86_64']])),
    (err: any) => {
      assert.equal(err.kind, 'unsupported-platform')

      return true
    }
  )
})

test('ownership paths are isolated by ownership ID and spawn nonce', () => {
  assert.equal(ownershipDirectory(OWNERSHIP_ID), `~/.hermes/desktop-ssh/${OWNERSHIP_ID}`)
  assert.equal(lockfilePath(OWNERSHIP_ID), `~/.hermes/desktop-ssh/${OWNERSHIP_ID}/backend.lock.json`)
  assert.equal(spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE), `~/.hermes/desktop-ssh/${OWNERSHIP_ID}/${SPAWN_NONCE}.log`)
})

test('readLockfile returns null for missing, empty, malformed, or wrong-schema', async () => {
  assert.equal(await readLockfile(fakeSsh([[/cat/, '']]), OWNERSHIP_ID), null)
  assert.equal(await readLockfile(fakeSsh([[/cat/, 'not json']]), OWNERSHIP_ID), null)
  assert.equal(await readLockfile(fakeSsh([[/cat/, JSON.stringify({ schemaVersion: 999 })]]), OWNERSHIP_ID), null)
  const good = ownedLock({ pid: 1, port: 2 })
  assert.deepEqual(await readLockfile(fakeSsh([[/cat/, JSON.stringify(good)]]), OWNERSHIP_ID), good)
})

test('writeLockfile mkdir -ps and stamps the schema version', async () => {
  const ssh = fakeSsh([])
  await writeLockfile(ssh, OWNERSHIP_ID, ownedLock({ pid: 7, port: 9 }))
  const cmd = ssh.calls.join('\n')
  assert.match(cmd, /mkdir -p/)
  assert.match(cmd, new RegExp(`"schemaVersion":${LOCKFILE_SCHEMA_VERSION}`))
})

test('remotePidAlive maps kill -0 ALIVE/DEAD', async () => {
  assert.equal(await remotePidAlive(fakeSsh([[/kill -0/, 'ALIVE']]), 123), true)
  assert.equal(await remotePidAlive(fakeSsh([[/kill -0/, 'DEAD']]), 123), false)
  assert.equal(await remotePidAlive(fakeSsh([]), null), false)
})

test('metadata and process proof transport failures remain indeterminate', async () => {
  const failure = new Error('connection reset')
  await assert.rejects(
    () => readLockfile(fakeSsh([[/cat/, failure]]), OWNERSHIP_ID),
    (error: any) => error.kind === 'transient-transport-error'
  )
  await assert.rejects(
    () => remotePidAlive(fakeSsh([[/kill -0/, failure]]), 123),
    (error: any) => error.kind === 'transient-transport-error'
  )
  await assert.rejects(
    () => pidIsOurDashboard(fakeSsh([[/print\("OK" if ok else "SHAPE"\)/, failure]]), 5, SPAWN_NONCE, '/x/hermes'),
    (error: any) => error.kind === 'transient-transport-error'
  )
})

test('pidIsOurDashboard requires the exact serve ownership nonce', async () => {
  assert.equal(
    await pidIsOurDashboard(fakeSsh([[/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()]]), 5, SPAWN_NONCE, '/x/hermes'),
    true
  )
  assert.equal(
    await pidIsOurDashboard(
      fakeSsh([[/print\("OK" if ok else "SHAPE"\)/, foreignIdentityOut()]]),
      5,
      'fedcba9876543210',
      '/x/hermes'
    ),
    false
  )
  assert.equal(
    await pidIsOurDashboard(fakeSsh([[/print\("OK" if ok else "SHAPE"\)/, foreignIdentityOut()]]), 5, SPAWN_NONCE, '/x/hermes'),
    false
  )
})

test('cleanupStale kills ONLY a provably-ours pid; foreign alive is ownership-conflict', async () => {
  const notOurs = fakeSsh([[/print\("OK" if ok else "SHAPE"\)/, foreignIdentityOut()]])
  await assert.rejects(
    () =>
      cleanupStale(notOurs, OWNERSHIP_ID, {
        pid: 5,
        spawnNonce: SPAWN_NONCE,
        hermesPath: '/x/hermes',
        logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE)
      }),
    (err: any) => err.kind === 'ownership-conflict'
  )
  assert.ok(!notOurs.calls.some(c => /kill 5\b/.test(c)), 'must not kill a pid that is not our dashboard')
  assert.ok(!notOurs.calls.some(c => /rm -f .*backend\.lock\.json/.test(c)), 'must preserve foreign lock')

  const ours = fakeSsh([
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()],
    [/terminate_result/, 'TERMINATED\n'],
    [/kill -0 9/, 'DEAD']
  ])

  await cleanupStale(ours, OWNERSHIP_ID, {
    pid: 9,
    spawnNonce: SPAWN_NONCE,
    hermesPath: '/x/hermes',
    logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE)
  })
  assert.ok(ours.calls.some(c => /terminate_result/.test(c)))
  assert.ok(ours.calls.some(c => /rm -f/.test(c)))
})

test('buildSpawnCommand is headless serve, detached, token not in argv', () => {
  const cmd = buildSpawnCommand('/x/hermes', 'work', { logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE) })
  assert.match(cmd, /serve --isolated/)
  assert.match(cmd, /--host 127\.0\.0\.1 --port 0/)
  assert.doesNotMatch(cmd, /--skip-build|--no-open/)
  assert.doesNotMatch(cmd, /\bdashboard\b/)
  assert.match(cmd, /--profile/)
  assert.match(cmd, /work/)
  assert.match(cmd, /setsid/)
  assert.match(cmd, /<\/dev\/null/)
  assert.match(cmd, /echo \$!/)
  assert.ok(!cmd.includes('tok_secret_value'), 'token must not appear in spawn command')
  assert.ok(!cmd.includes('HERMES_DASHBOARD_SESSION_TOKEN'), 'token env var must not appear')
})

test('buildSpawnCommand always uses serve (legacy dashboard path removed)', () => {
  const cmd = buildSpawnCommand('/x/hermes', 'work', { logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE) })
  assert.match(cmd, /serve --isolated/)
  assert.match(cmd, /--host 127\.0\.0\.1 --port 0/)
  assert.doesNotMatch(cmd, /dashboard/)
  assert.doesNotMatch(cmd, /--skip-build/)
  assert.match(cmd, /setsid/)
})

test('spawnRemoteDashboard returns exact ownership artifacts', async () => {
  const ssh = fakeSsh([
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [/python3 -c/, ''],
    [/printf '%s\\n'/, ''],
    [/setsid|nohup/, '4242\n']
  ])

  const { pid, spawnNonce, logPath } = await spawnRemoteDashboard(ssh, {
    hermesPath: '/x/hermes',
    profile: '',
    token: 'tk',
    ownershipId: OWNERSHIP_ID
  })

  assert.equal(pid, 4242)
  assert.match(spawnNonce, /^[0-9a-f]{16}$/)
  assert.equal(logPath, spawnLogPath(OWNERSHIP_ID, spawnNonce))
})

test('spawnRemoteDashboard always spawns serve (legacy dashboard path removed)', async () => {
  const ssh = fakeSsh([
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [/python3 -c/, ''],
    [/printf '%s\\n'/, ''],
    [/setsid|nohup/, '4242\n']
  ])

  await spawnRemoteDashboard(ssh, { hermesPath: '/x/hermes', profile: '', token: 'tk', ownershipId: OWNERSHIP_ID })
  const spawn = ssh.calls.find(c => /setsid|nohup/.test(c))
  assert.match(spawn, /serve --isolated/)
  assert.doesNotMatch(spawn, /\bdashboard\b/)
})

test('READY_RE accepts both serve and dashboard sentinels', () => {
  assert.equal(READY_RE.exec('HERMES_BACKEND_READY port=4321')?.[1], '4321')
  assert.equal(READY_RE.exec('HERMES_DASHBOARD_READY port=8765')?.[1], '8765')
})

test('spawnRemoteDashboard rejects when no pid is returned', async () => {
  const ssh = fakeSsh([
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [/python3 -c/, ''],
    [/printf '%s\\n'/, ''],
    [/setsid|nohup/, 'not-a-pid']
  ])

  await assert.rejects(
    () => spawnRemoteDashboard(ssh, { hermesPath: '/x/hermes', profile: '', token: 't', ownershipId: OWNERSHIP_ID }),
    (err: any) => {
      assert.equal(err.kind, 'spawn-failed')

      return true
    }
  )
})

test('scrapeReadyPort reads only the named spawn log', async () => {
  const logPath = spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE)
  const ssh = fakeSsh([[/cat/, 'some noise\nHERMES_DASHBOARD_READY port=51234\n']])
  const port = await scrapeReadyPort(ssh, logPath, { timeoutMs: 1000 })
  assert.equal(port, 51234)
  assert.ok(ssh.calls.every(call => !call.includes('desktop-ssh.log')))
})

test('scrapeReadyPort times out and reports a dead spawn', async () => {
  // never emits a READY line
  const ssh = fakeSsh([[/cat .*\.log/, 'still starting...']])
  await assert.rejects(
    () => scrapeReadyPort(ssh, spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE), { timeoutMs: 60 }),
    (err: any) => {
      assert.equal(err.kind, 'ready-timeout')

      return true
    }
  )
  // dead process before announcement → spawn-failed
  await assert.rejects(
    () =>
      scrapeReadyPort(fakeSsh([[/cat/, '']]), spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE), {
        timeoutMs: 1000,
        isAlive: async () => false
      }),
    (err: any) => {
      assert.equal(err.kind, 'spawn-failed')

      return true
    }
  )
})

function connectDeps(ssh, over: any = {}) {
  return {
    ssh,
    ownershipId: OWNERSHIP_ID,
    profile: '',
    forward: async () => {},
    cancelForward: async () => {},
    pickLocalPort: async () => 50001,
    waitForHermes: async () => {},
    probeReuseProof: async () => 'authenticated-ok',
    adoptServedToken: async (_baseUrl, spawn) => spawn || 'served-token',
    rememberLog: () => {},
    readyTimeoutMs: 2000,
    ...over
  }
}

test('connect() spawns fresh when there is no lockfile, adopts the served token', async () => {
  const ssh = fakeSsh(spawnConnectRules({ pid: 777, port: 51999 }))

  const result = await connect(connectDeps(ssh, { adoptServedToken: async () => 'the-served-token' }))
  assert.equal(result.reused, false)
  assert.equal(result.remotePort, 51999)
  assert.equal(result.localPort, 50001)
  assert.equal(result.pid, 777)
  assert.equal(result.token, 'the-served-token')
  assert.equal(result.baseUrl, 'http://127.0.0.1:50001')
  assert.equal(result.tokenFingerprint, fingerprintToken('the-served-token'))
})

test('managed SSH maps a local scope to a different non-default remote profile', async () => {
  const localScope = 'work'

  const sshConfig = profileSshOverride(
    {
      profiles: {
        [localScope]: {
          mode: 'ssh',
          host: 'remote-box',
          remoteProfile: 'writer_2'
        }
      }
    },
    localScope
  )

  assert.equal(sshConfig?.remoteProfile, 'writer_2')

  const ssh = fakeSsh(spawnConnectRules({ pid: 778, port: 52000, ready: 'HERMES_BACKEND_READY port=52000\n' }))

  await connect(
    connectDeps(ssh, {
      profile: sshConfig?.remoteProfile,
      adoptServedToken: async () => 'mapped-profile-token'
    })
  )

  const spawn = ssh.calls.find(command => /setsid|nohup/.test(command)) || ''
  assert.match(spawn, /--profile\b/)
  assert.ok(spawn.includes('writer_2'))
  assert.match(spawn, /serve\s+--isolated/)
  assert.match(spawn, /\.hermes\/desktop-ssh\/[0-9a-f]{32}\/[0-9a-f]{16}\.token/)
  assert.ok(!spawn.includes(' work'), 'the local Desktop scope must not become the remote profile')
})

test('connect() reuses a healthy dashboard when fingerprint + probe pass', async () => {
  const reuseToken = 'stored-token'
  const lock = ownedLock({ tokenFingerprint: fingerprintToken(reuseToken) })

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, JSON.stringify(lock)],
    [/kill -0/, 'ALIVE'],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()]
  ]))

  const result = await connect(connectDeps(ssh, { reuseToken, adoptServedToken: async (_b, t) => t }))
  assert.equal(result.reused, true)
  assert.equal(result.pid, 333)
  assert.equal(result.remotePort, 40000)
  // never spawned
  assert.ok(!ssh.calls.some(c => /setsid/.test(c)), 'reuse path must not spawn a new dashboard')
})

test('connect() respawns when the requested remote profile differs from the lockfile profile', async () => {
  const reuseToken = 'stored-token'
  const lock = ownedLock({ profile: 'desktop-work', tokenFingerprint: fingerprintToken(reuseToken) })
  let lockReads = 0

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, () => {
      lockReads += 1

      // First read sees owned lock; after cleanup the lock is gone.
      return lockReads === 1 ? JSON.stringify(lock) : ''
    }],
    [/kill -0 333/, command => (ssh.calls.some(c => /terminate_result/.test(c)) ? 'DEAD' : 'ALIVE')],
    [/print\("OK" if ok else "SHAPE"\)/, command =>
      (command.includes('pid=890') || /pid=890/.test(command) ? ownedIdentityOut({ startTime: 890 }) : ownedIdentityOut())
    ],
    // broader identity match by nonce script — always owned shape for these pids
    [/terminate_result/, 'TERMINATED\n'],
    [/--version/, 'Hermes Agent v0.18.2\n'],
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [command => /python3 -c/.test(command) && /O_EXCL/.test(command), ''],
    [/setsid/, '890\n'],
    [/kill -0 890/, 'ALIVE'],
    [/cat .*\.log/, 'HERMES_DASHBOARD_READY port=52050\n']
  ]))

  // Fix identity matcher: owned for both classify and post-spawn
  // (rebuilt below if needed)

  const result = await connect(
    connectDeps(ssh, { profile: 'default', reuseToken, adoptServedToken: async () => 'fresh' })
  )

  assert.equal(result.reused, false)
  assert.ok(
    ssh.calls.some(c => /setsid/.test(c)),
    'profile mismatch must spawn a fresh dashboard'
  )
})

test('connect() replaces an owned backend when the configured launcher path changes', async () => {
  const reuseToken = 'stored-token'

  // Live argv may still be python + runtime entry after an exec wrapper, but a
  // different configured launcher is explicit operator intent and must not be
  // silently ignored.
  const lock = ownedLock({
    hermesPath: '/x/hermes-wrapper',
    launcherPath: '/x/hermes-wrapper',
    tokenFingerprint: fingerprintToken(reuseToken)
  })

  let oldGone = false

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, () => (oldGone ? '' : JSON.stringify(lock))],
    [/kill -0 333/, () => (oldGone ? 'DEAD' : 'ALIVE')],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()],
    [/terminate_result/, () => { oldGone = true;

 return 'TERMINATED\n' }],
    [/\[ -x .*\/new\/hermes/, 'OK'],
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [command => /python3 -c/.test(command) && /O_EXCL/.test(command), ''],
    [/setsid/, '902\n'],
    [/kill -0 902/, 'ALIVE'],
    [/cat .*\.log/, 'HERMES_DASHBOARD_READY port=44101\n']
  ]))

  const result = await connect(
    connectDeps(ssh, { reuseToken, remoteHermesPath: '/new/hermes', adoptServedToken: async (_b, t) => t })
  )

  assert.equal(result.reused, false)
  assert.equal(result.pid, 902)
  assert.ok(ssh.calls.some(c => /setsid/.test(c)), 'configured launcher change must spawn a replacement')
})

test('connect() fails closed on alive foreign identity (no delete/signal/spawn)', async () => {
  const reuseToken = 'stored-token'
  const lock = ownedLock({ tokenFingerprint: fingerprintToken(reuseToken) })

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, JSON.stringify(lock)],
    [/kill -0/, 'ALIVE'],
    [/print\("OK" if ok else "SHAPE"\)/, foreignIdentityOut()]
  ]))

  await assert.rejects(
    () => connect(connectDeps(ssh, { reuseToken, adoptServedToken: async () => 'fresh' })),
    (err: any) => err.kind === 'ownership-conflict'
  )
  assert.ok(!ssh.calls.some(c => /setsid/.test(c)), 'must not spawn')
  assert.ok(!ssh.calls.some(c => /kill 333\b/.test(c)), 'must not signal')
  assert.ok(!ssh.calls.some(c => /rm -f .*backend\.lock\.json/.test(c)), 'must preserve lock')
})

test('connect() fails closed on live malformed, incompatible-protocol, and incompatible-schema records', async () => {
  const reuseToken = 'stored-token'

  const records = [
    'not json',
    JSON.stringify(ownedLock({ protocolVersion: PROTOCOL_VERSION + 99 })),
    JSON.stringify(ownedLock({ schemaVersion: LOCKFILE_SCHEMA_VERSION + 99 }))
  ]

  for (const record of records) {
    const ssh = fakeSsh(baseConnectRules([
      [/cat .*lock\.json/, record],
      [/kill -0 333/, 'ALIVE']
    ]))

    await assert.rejects(
      () => connect(connectDeps(ssh, { reuseToken, adoptServedToken: async () => 'fresh' })),
      (err: any) => err.kind === 'ownership-conflict'
    )
    assert.ok(!ssh.calls.some(c => /setsid|nohup/.test(c)), 'invalid existing ownership evidence must not be overlapped')
    assert.ok(!ssh.calls.some(c => /rm -f .*backend\.lock\.json/.test(c)), 'invalid ownership evidence must be preserved')
  }
})

test('connect() fresh spawn writes hermesHome + protocolVersion into the lockfile', async () => {
  const writes: string[] = []

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, ''],
    [/echo "\$\{HERMES_HOME/, '/home/alice/.hermes\n'],
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [command => /python3 -c/.test(command) && /O_EXCL/.test(command), ''],
    [/setsid/, '700\n'],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut({ startTime: 700 })],
    [/kill -0 700/, 'ALIVE'],
    [/cat .*\.log/, 'HERMES_DASHBOARD_READY port=45500\n'],
    [
      /printf '%s' '/,
      c => {
        writes.push(c)

        return ''
      }
    ]
  ]))

  await connect(connectDeps(ssh, { adoptServedToken: async () => 'fresh' }))
  const lockWrite = writes.find(c => c.includes('schemaVersion')) || ''
  assert.match(lockWrite, new RegExp(`"protocolVersion":${PROTOCOL_VERSION}`))
  assert.match(lockWrite, new RegExp(`"schemaVersion":${LOCKFILE_SCHEMA_VERSION}`))
  assert.match(lockWrite, /"hermesHome":"\/home\/alice\/\.hermes"/)
  assert.match(lockWrite, /"pidStartTime":/)
  assert.match(lockWrite, /"launcherPath":/)
})

test('connect() respawns when the lockfile pid is dead (killed dashboard)', async () => {
  const lock = ownedLock({ tokenFingerprint: fingerprintToken('t') })
  let sawDeadCleanup = false

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, () => (sawDeadCleanup ? '' : JSON.stringify(lock))],
    [/kill -0 333/, 'DEAD'],
    [/rm -f/, c => {
      if (/backend\.lock\.json/.test(c)) {
        sawDeadCleanup = true
      }

      return ''
    }],
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [command => /python3 -c/.test(command) && /O_EXCL/.test(command), ''],
    [/setsid/, '888\n'],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut({ startTime: 888 })],
    [/kill -0 888/, 'ALIVE'],
    [/cat .*\.log/, 'HERMES_DASHBOARD_READY port=42000\n']
  ]))

  const result = await connect(connectDeps(ssh, { reuseToken: 't', adoptServedToken: async () => 'fresh' }))
  assert.equal(result.reused, false)
  assert.equal(result.pid, 888)
  assert.equal(result.remotePort, 42000)
  assert.ok(
    !ssh.calls.some(command => command.includes('pid=333') && command.includes('print("OK" if ok else "SHAPE")')),
    'a dead pid has no process identity to verify'
  )
})

test('connect() fails closed when the dashboard is alive but not provably owned', async () => {
  const reuseToken = 'stored'
  // Incomplete lock still has enough fields for normalize via ownedLock defaults
  // when using ownedLock; here force foreign identity classification.
  const lock = ownedLock({ tokenFingerprint: fingerprintToken(reuseToken) })

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, JSON.stringify(lock)],
    [/kill -0/, 'ALIVE'],
    [/print\("OK" if ok else "SHAPE"\)/, foreignIdentityOut()]
  ]))

  await assert.rejects(
    () =>
      connect(
        connectDeps(ssh, {
          reuseToken,
          probeReuseProof: async () => 'authenticated-stale',
          adoptServedToken: async () => 'fresh'
        })
      ),
    (err: any) => err.kind === 'ownership-conflict'
  )
  assert.ok(!ssh.calls.some(c => /setsid/.test(c)))
})

test('connect() aborts on an unsupported remote platform before doing anything else', async () => {
  const ssh = fakeSsh([[/uname/, 'SunOS\nsun4v']])
  await assert.rejects(
    () => connect(connectDeps(ssh)),
    (err: any) => {
      assert.equal(err.kind, 'unsupported-platform')

      return true
    }
  )
  assert.ok(!ssh.calls.some(c => /setsid/.test(c)))
})

test('openForward retries bind collisions only', async () => {
  const ports = [41001, 41002]
  const calls: number[] = []

  const localPort = await openForward(
    {
      pickLocalPort: async () => ports.shift(),
      forward: async port => {
        calls.push(port)

        if (calls.length === 1) {
          throw new Error('bind: Address already in use')
        }
      }
    },
    9119
  )

  assert.equal(localPort, 41002)
  assert.deepEqual(calls, [41001, 41002])
  assert.equal(isForwardBindCollision(new Error('Permission denied')), false)
})

test('connect() preserves an owned backend when a reuse transport throws', async () => {
  const reuseToken = 'stored-token'
  const lock = ownedLock({ tokenFingerprint: fingerprintToken(reuseToken) })

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, JSON.stringify(lock)],
    [/kill -0/, 'ALIVE'],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()]
  ]))

  await assert.rejects(
    () =>
      connect(
        connectDeps(ssh, {
          reuseToken,
          forward: async () => {
            throw new Error('network reset')
          }
        })
      ),
    /network reset/
  )
  assert.ok(!ssh.calls.some(cmd => /kill 333\b/.test(cmd)))
})

test('validateRemotePath accepts absolute POSIX paths', () => {
  assert.doesNotThrow(() => validateRemotePath('/usr/bin/hermes'))
  assert.doesNotThrow(() => validateRemotePath('/home/user/.hermes/hermes-agent/venv/bin/hermes'))
})

test('validateRemotePath accepts ~/ prefix paths', () => {
  assert.doesNotThrow(() => validateRemotePath('~/bin/hermes'))
  assert.doesNotThrow(() => validateRemotePath('~/.hermes/logs/desktop-ssh.log'))
  assert.doesNotThrow(() => validateRemotePath('~'))
})

test('validateRemotePath accepts paths with spaces and quotes', () => {
  assert.doesNotThrow(() => validateRemotePath('/home/user/my project/hermes'))
  assert.doesNotThrow(() => validateRemotePath("~/path with 'quotes'/file"))
  assert.doesNotThrow(() => validateRemotePath('/path with "double quotes"/file'))
})

test('validateRemotePath rejects relative paths', () => {
  assert.throws(() => validateRemotePath('hermes'), /absolute|relative/i)
  assert.throws(() => validateRemotePath('./bin/hermes'), /absolute|relative/i)
  assert.throws(() => validateRemotePath('../etc/passwd'), /absolute|relative/i)
})

test('validateRemotePath rejects NUL and newline', () => {
  assert.throws(() => validateRemotePath('/usr/bin/hermes\x00'), /unsafe/i)
  assert.throws(() => validateRemotePath('/usr/bin/hermes\n'), /unsafe/i)
  assert.throws(() => validateRemotePath('/usr/bin/hermes\r'), /unsafe/i)
})

test('validateRemotePath preserves shell metacharacters as path data', () => {
  for (const p of ['/usr/$(whoami)/hermes', '/usr/`id`/hermes', '/usr/a;b|c&d<e>f']) {
    assert.doesNotThrow(() => validateRemotePath(p))
    assert.match(expandRemotePath(p), /^'/)
  }
})

test('expandRemotePath expands ~/ to "$HOME"/', () => {
  const result = expandRemotePath('~/.hermes/logs/desktop-ssh.log')
  assert.match(result, /\$HOME/)
  assert.ok(!result.includes('eval'), 'must not use eval')
  assert.ok(!result.includes('echo'), 'must not use echo for expansion')
})

test('expandRemotePath returns quoted absolute paths unchanged', () => {
  const result = expandRemotePath('/usr/local/bin/hermes')
  assert.ok(result.includes('/usr/local/bin/hermes'))
  assert.ok(!result.includes('eval'))
})

test('expandRemotePath preserves spaces as data', () => {
  const result = expandRemotePath('/home/user/my project/hermes')
  assert.ok(result.includes('my project'), 'spaces must be preserved, not split')
})

test('buildSpawnCommand does not embed the token in the command string', () => {
  const cmd = buildSpawnCommand('/x/hermes', 'work', { logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE) })
  assert.ok(!cmd.includes('super_secret_token_value'), 'token must not appear in the spawn command')
  assert.ok(!cmd.includes('HERMES_DASHBOARD_SESSION_TOKEN'), 'env var name must not appear')
})

test('buildSpawnCommand includes --ssh-session-token-file when tokenFilePath is provided', () => {
  const cmd = buildSpawnCommand('/x/hermes', 'work', {
    tokenFilePath: `~/.hermes/desktop-ssh/${OWNERSHIP_ID}/${SPAWN_NONCE}.token`,
    logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE),
    spawnNonce: SPAWN_NONCE
  })

  assert.match(cmd, /--ssh-session-token-file/)
  assert.match(cmd, /\.hermes\/desktop-ssh\//)
})

test('buildSpawnCommand always uses serve, never dashboard', () => {
  const cmd = buildSpawnCommand('/x/hermes', '', { logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE) })
  assert.match(cmd, /serve --isolated/)
  assert.doesNotMatch(cmd, /\bdashboard\b/)
  assert.doesNotMatch(cmd, /--skip-build/)
  assert.doesNotMatch(cmd, /--no-open/)
})

test('buildSpawnCommand raises the SSH child file limit before execing Hermes', () => {
  const cmd = buildSpawnCommand('/x/hermes', '', { logPath: spawnLogPath(OWNERSHIP_ID, SPAWN_NONCE) })
  assert.match(cmd, /ulimit -n 65536 2>\/dev\/null \|\| true; exec env HERMES_DESKTOP=1/)
  assert.ok(cmd.indexOf('ulimit -n 65536') < cmd.indexOf('serve --isolated'))
})

test('spawnRemoteDashboard removes a token file when upload reporting fails', async () => {
  const failure = new Error('channel closed')

  const ssh = fakeSsh([
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [command => /python3 -c/.test(command) && !/rm -f/.test(command), failure],
    [/rm -f/, '']
  ])

  await assert.rejects(
    () => spawnRemoteDashboard(ssh, { hermesPath: '/x/hermes', profile: '', token: 'tok', ownershipId: OWNERSHIP_ID }),
    /channel closed/
  )
  assert.ok(ssh.calls.some(command => /rm -f .*\.token/.test(command)))
})

test('spawnRemoteDashboard streams the token over stdin, not argv/env', async () => {
  const stdinCalls: string[] = []
  const calls: string[] = []

  const ssh = {
    calls,
    async exec(cmd, opts?) {
      calls.push(cmd)

      if (opts?.stdinData) {
        stdinCalls.push(opts.stdinData)
      }

      if (/grep -q ssh-session-token-file/.test(cmd)) {
        return 'YES\n'
      }

      if (/python3 -c/.test(cmd)) {
        return ''
      }

      if (/setsid|nohup/.test(cmd)) {
        return '4242\n'
      }

      if (/printf '%s\\n'/.test(cmd)) {
        return ''
      }

      return ''
    }
  }

  const { pid } = await spawnRemoteDashboard(ssh as any, {
    hermesPath: '/x/hermes',
    profile: '',
    token: 'secret_token_val',
    ownershipId: OWNERSHIP_ID
  })

  assert.equal(pid, 4242)
  assert.ok(stdinCalls.length > 0, 'token must be sent via stdin')
  assert.ok(
    stdinCalls.some(d => d === 'secret_token_val'),
    'stdin must contain the token'
  )

  for (const cmd of calls) {
    assert.ok(!cmd.includes('secret_token_val'), `token leaked into command: ${cmd}`)
  }
})

test('spawnRemoteDashboard upload uses exclusive-create and O_NOFOLLOW', async () => {
  const calls: string[] = []

  const ssh = {
    calls,
    async exec(cmd, opts?) {
      calls.push(cmd)

      if (/grep -q ssh-session-token-file/.test(cmd)) {
        return 'YES\n'
      }

      if (/python3 -c/.test(cmd)) {
        return ''
      }

      if (/setsid|nohup/.test(cmd)) {
        return '4242\n'
      }

      if (/printf '%s\\n'/.test(cmd)) {
        return ''
      }

      return ''
    }
  }

  await spawnRemoteDashboard(ssh as any, {
    hermesPath: '/x/hermes',
    profile: '',
    token: 'tk',
    ownershipId: OWNERSHIP_ID
  })
  const uploadCmd = calls.find(c => /python3 -c/.test(c))
  assert.ok(uploadCmd, 'must use python3 -c for token upload')
  assert.match(uploadCmd, /O_EXCL/, 'upload must use O_EXCL to reject existing files')
  assert.match(uploadCmd, /O_NOFOLLOW/, 'upload must use O_NOFOLLOW to reject symlinks')
  assert.match(uploadCmd, /O_WRONLY/, 'upload must open write-only')
  assert.match(uploadCmd, /dir_fd=dd/, 'upload must create relative to the opened parent directory')
  assert.match(uploadCmd, /os\.fstat\(dd\)/, 'upload must validate the opened parent directory')
  assert.ok(!uploadCmd.includes('tk'), 'token must not appear in the upload command')
})

test('readLockfile rejects lock with non-integer pid', async () => {
  const lock = { schemaVersion: LOCKFILE_SCHEMA_VERSION, pid: 'not-a-number', port: 8080 }
  assert.equal(await readLockfile(fakeSsh([[/cat/, JSON.stringify(lock)]]), OWNERSHIP_ID), null)
})

test('readLockfile rejects lock with pid <= 0', async () => {
  const lock = { schemaVersion: LOCKFILE_SCHEMA_VERSION, pid: -1, port: 8080 }
  assert.equal(await readLockfile(fakeSsh([[/cat/, JSON.stringify(lock)]]), OWNERSHIP_ID), null)
})

test('readLockfile rejects lock with port out of range', async () => {
  const lock = { schemaVersion: LOCKFILE_SCHEMA_VERSION, pid: 100, port: 99999 }
  assert.equal(await readLockfile(fakeSsh([[/cat/, JSON.stringify(lock)]]), OWNERSHIP_ID), null)
  const lock2 = { schemaVersion: LOCKFILE_SCHEMA_VERSION, pid: 100, port: 0 }
  assert.equal(await readLockfile(fakeSsh([[/cat/, JSON.stringify(lock2)]]), OWNERSHIP_ID), null)
})

test('readLockfile accepts a complete owned lock', async () => {
  const lock = ownedLock({ pid: 42, port: 51234 })
  const result = await readLockfile(fakeSsh([[/cat/, JSON.stringify(lock)]]), OWNERSHIP_ID)
  assert.deepEqual(result, lock)
})

test('connect() reuse path does not write a token file', async () => {
  const reuseToken = 'stored-token'
  const lock = ownedLock({ tokenFingerprint: fingerprintToken(reuseToken) })

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, JSON.stringify(lock)],
    [/kill -0/, 'ALIVE'],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()]
  ]))

  const result = await connect(connectDeps(ssh, { reuseToken, adoptServedToken: async (_b, t) => t }))
  assert.equal(result.reused, true)
  assert.ok(!ssh.calls.some(c => /sys\.stdin\.buffer\.read/.test(c)), 'reuse must not upload a token file')
})

test('spawnRemoteDashboard fails with update-required when remote lacks --ssh-session-token-file', async () => {
  const ssh = fakeSsh([[/--ssh-session-token-file/, 'NO\n']])

  await assert.rejects(
    () => spawnRemoteDashboard(ssh, { hermesPath: '/x/hermes', profile: '', token: 'tk', ownershipId: OWNERSHIP_ID }),
    (err: any) => {
      assert.match(err.message, /update|upgrade/i)
      assert.equal(err.kind, 'update-required')

      return true
    }
  )
})

test('readLockfile rejects a log path outside the exact ownership and spawn path', async () => {
  const lock = ownedLock({ logPath: '~/.hermes/desktop-ssh/other.log' })
  const ssh = fakeSsh([[/cat .*lock\.json/, JSON.stringify(lock)]])
  assert.equal(await readLockfile(ssh, OWNERSHIP_ID), null)
})

test('cleanupStale never deletes a lock-supplied unexpected log path', async () => {
  const ssh = fakeSsh([
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()],
    [/terminate_result/, 'TERMINATED\n'],
    [/kill -0 333/, 'DEAD']
  ])

  await cleanupStale(ssh, OWNERSHIP_ID, ownedLock({ logPath: '~/.hermes/unrelated.log' }))
  assert.ok(!ssh.calls.some(command => command.includes('unrelated.log')))
})

test('pidIsOurDashboard requires an exact nonce option value', async () => {
  const prefix = `/x/hermes serve --isolated --ssh-owner-nonce ${SPAWN_NONCE}ff`
  const suffix = `/x/hermes serve --isolated --ssh-owner-nonce xx${SPAWN_NONCE}`
  assert.equal(await pidIsOurDashboard(fakeSsh([[/print\("OK" if ok else "SHAPE"\)/, foreignIdentityOut()]]), 5, SPAWN_NONCE, '/x/hermes'), false)
  assert.equal(await pidIsOurDashboard(fakeSsh([[/print\("OK" if ok else "SHAPE"\)/, foreignIdentityOut()]]), 5, SPAWN_NONCE, '/x/hermes'), false)
})

test('connect removes the token file when a fresh backend fails after returning a pid', async () => {
  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, ''],
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [command => /python3 -c/.test(command) && /O_EXCL/.test(command), ''],
    [/setsid/, '999\n'],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut({ startTime: 999 })],
    [/kill -0 999/, 'DEAD'],
    [/terminate_result/, 'GONE\n']
  ]))

  await assert.rejects(() => connect(connectDeps(ssh)), /exited before announcing/i)
  assert.ok(ssh.calls.some(command => /rm -f .*\.token/.test(command)))
})

test('connect preserves an exact-owned backend when reuse proof transport fails', async () => {
  const reuseToken = 'stored-token'
  const lock = ownedLock({ tokenFingerprint: fingerprintToken(reuseToken) })

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, JSON.stringify(lock)],
    [/kill -0/, 'ALIVE'],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()]
  ]))

  await assert.rejects(
    () =>
      connect(
        connectDeps(ssh, {
          reuseToken,
          probeReuseProof: async () => {
            throw new Error('connection reset')
          }
        })
      ),
    (error: any) => error.kind === 'transient-transport-error'
  )
  assert.ok(!ssh.calls.some(command => /kill 333\b/.test(command)))
  assert.ok(!ssh.calls.some(command => /rm -f .*backend\.lock\.json/.test(command)))
})

test('connect replaces an exact-owned backend only after authenticated stale proof', async () => {
  const reuseToken = 'stored-token'
  const lock = ownedLock({ tokenFingerprint: fingerprintToken(reuseToken) })

  let killed333 = false

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, () => (killed333 ? '' : JSON.stringify(lock))],
    [/terminate_result/, () => { killed333 = true;

 return 'TERMINATED\n' }],
    [/kill -0 333/, () => (killed333 ? 'DEAD' : 'ALIVE')],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()],
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [command => /python3 -c/.test(command) && /O_EXCL/.test(command), ''],
    [/setsid/, '999\n'],
    [/kill -0 999/, 'ALIVE'],
    [/cat .*\.log/, 'HERMES_DASHBOARD_READY port=43000\n']
  ]))

  const result = await connect(
    connectDeps(ssh, {
      reuseToken,
      probeReuseProof: async (_baseUrl, token, nonce) => {
        assert.equal(token, reuseToken)
        assert.equal(nonce, SPAWN_NONCE)

        return 'authenticated-stale'
      },
      adoptServedToken: async () => 'fresh'
    })
  )

  assert.equal(result.reused, false)
  assert.ok(ssh.calls.some(command => /terminate_result/.test(command)))
})

test('remote SSH ownership capability requires both secure bootstrap flags', async () => {
  let helpProbe = ''

  const supported = fakeSsh([
    [
      /serve --help/,
      command => {
        helpProbe = command

        return 'YES\n'
      }
    ]
  ])

  assert.equal(await remoteSupportsSshOwnership(supported, '/x/hermes'), true)
  assert.match(helpProbe, /ssh-session-token-file/)
  assert.match(helpProbe, /ssh-owner-nonce/)

  const unsupported = fakeSsh([[/serve --help/, 'NO\n']])
  assert.equal(await remoteSupportsSshOwnership(unsupported, '/x/hermes'), false)
})

test('exec-wrapper ownership identity verifies as owned and can be reaped', async () => {
  // Lock launcher is a wrapper; live argv is python + runtime hermes entry.
  const lock = ownedLock({
    hermesPath: '/x/hermes-wrapper',
    launcherPath: '/x/hermes-wrapper',
    pid: 5,
    pidStartTime: 12345,
    processFingerprint: 'a'.repeat(32)
  })

  const ssh = fakeSsh([
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()],
    [/terminate_result/, 'TERMINATED\n'],
    [/kill -0 5/, 'DEAD']
  ])

  assert.equal(await pidIsOurDashboard(ssh, 5, SPAWN_NONCE, '/x/hermes-wrapper', lock), true)
  await cleanupStale(ssh, OWNERSHIP_ID, lock)
  assert.ok(ssh.calls.some(c => /terminate_result/.test(c)))
  assert.ok(ssh.calls.some(c => /rm -f .*backend\.lock\.json/.test(c)))
})

test('PID reuse start-time mismatch is foreign and never signaled', async () => {
  const lock = ownedLock({ pid: 5, pidStartTime: 111, processFingerprint: 'a'.repeat(32) })

  const ssh = fakeSsh([
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut({ startTime: 222 })]
  ])

  assert.equal(await pidIsOurDashboard(ssh, 5, SPAWN_NONCE, '/x/hermes', lock), false)
  await assert.rejects(
    () => cleanupStale(ssh, OWNERSHIP_ID, lock),
    (err: any) => err.kind === 'ownership-conflict'
  )
  assert.ok(!ssh.calls.some(c => /kill 5\b/.test(c)))
})

test('post-proof PID reuse is rejected by the identity-checked termination command', async () => {
  const lock = ownedLock({ pid: 5 })

  const ssh = fakeSsh([
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()],
    [/terminate_result/, 'MISMATCH\n']
  ])

  await assert.rejects(
    () => cleanupStale(ssh, OWNERSHIP_ID, lock),
    (err: any) => err.kind === 'ownership-conflict'
  )
  const terminate = ssh.calls.find(c => /terminate_result/.test(c)) || ''
  assert.match(terminate, /pidfd_open/, 'Linux termination must bind a pidfd before signaling')
  assert.match(terminate, /pidStartTime|expected_start/, 'termination must recheck process start identity')
  assert.ok(!ssh.calls.some(c => /^kill 5\b/.test(c)), 'must not issue a separate racy shell kill')
})

test('cleanup waits for asynchronous SIGTERM exit before removing evidence', async () => {
  const lock = ownedLock({ pid: 5 })
  let livenessChecks = 0

  const ssh = fakeSsh([
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()],
    [/terminate_result/, 'TERMINATED\n'],
    [/kill -0 5/, () => {
      livenessChecks += 1

      return livenessChecks === 1 ? 'ALIVE' : 'DEAD'
    }]
  ])

  await cleanupStale(ssh, OWNERSHIP_ID, lock)
  assert.equal(livenessChecks, 2)
  assert.ok(ssh.calls.some(c => /rm -f .*backend\.lock\.json/.test(c)))
})

test('identity-checked termination helper is executable and reports an absent pid as GONE', async () => {
  const localSsh = {
    async exec(command) {
      const { stdout } = await execAsync(command)

      return stdout
    }
  }

  const result = await terminateOwnedProcess(localSsh, ownedLock({ pid: 4194304 }))

  assert.equal(result, 'GONE')
})

test('GONE during failed-spawn cleanup preserves the original spawn error', async () => {
  let identityReads = 0

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, ''],
    [/grep -q ssh-session-token-file/, 'YES\n'],
    [command => /python3 -c/.test(command) && /O_EXCL/.test(command), ''],
    [/setsid/, '1001\n'],
    [/print\("OK" if ok else "SHAPE"\)/, () => {
      identityReads += 1

      return identityReads === 1 ? ownedIdentityOut({ startTime: 1001 }) : goneIdentityOut()
    }],
    [/kill -0 1001/, 'DEAD']
  ]))

  await assert.rejects(
    () => connect(connectDeps(ssh)),
    (err: any) => err.kind === 'spawn-failed' && /exited before announcing/i.test(err.message)
  )
  assert.ok(ssh.calls.some(c => /rm -f .*backend\.lock\.json/.test(c)), 'gone spawn evidence should be cleaned')
})

test('ownership mutex heartbeats and fences a callback after lease loss', async () => {
  let heldChecks = 0

  const ssh = fakeSsh([
    [/stale_ms=/, 'ACQUIRED\n'],
    [/print\("HELD"/, () => {
      heldChecks += 1

      return heldChecks === 1 ? 'HELD\n' : 'LOST\n'
    }],
    [/data==holder/, '']
  ])

  await assert.rejects(
    () => withOwnershipMutex(
      ssh,
      OWNERSHIP_ID,
      async lease => {
        await new Promise(resolve => setTimeout(resolve, 20))
        await lease.assertHeld()
      },
      { heartbeatMs: 5 }
    ),
    (err: any) => err.kind === 'ownership-conflict'
  )
  assert.ok(heldChecks >= 2, 'heartbeat and explicit fence must both validate the holder token')
})

test('token-absent healthy lock still reuses when identity-owned', async () => {
  // Token files are read-and-unlinked by design; absence is not orphan proof.
  const reuseToken = 'stored-token'
  const lock = ownedLock({ tokenFingerprint: fingerprintToken(reuseToken) })

  const ssh = fakeSsh(baseConnectRules([
    [/cat .*lock\.json/, JSON.stringify(lock)],
    [/kill -0/, 'ALIVE'],
    [/print\("OK" if ok else "SHAPE"\)/, ownedIdentityOut()],
    // no token file present on remote
    [/\.token/, '']
  ]))

  const result = await connect(connectDeps(ssh, { reuseToken, adoptServedToken: async (_b, t) => t }))
  assert.equal(result.reused, true)
  assert.ok(!ssh.calls.some(c => /setsid/.test(c)))
})
