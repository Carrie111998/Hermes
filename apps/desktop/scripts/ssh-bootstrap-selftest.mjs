/**
 * End-to-end self-test of the remote program in electron/posix-remote-bootstrap.ts.
 *
 *   node scripts/ssh-bootstrap-selftest.mjs <ssh-host>
 *
 * The unit tests mock ssh, so they answer whatever command they are given and
 * cannot see a mistake in the program that actually runs on the remote (an
 * off-by-one argv index shipped past a full green suite exactly that way).
 * This drives the real program over a real ssh connection, against a throw-away
 * HOME under /tmp with a FAKE hermes, and checks what the remote host actually
 * did: cold spawn, reuse, an owned respawn, a foreign pid left alone, a bounded
 * ready-timeout with full cleanup, 0600/0700 token handling, and the token
 * never appearing in argv or the process list.
 *
 * It touches nothing real on the remote: no ~/.hermes, no running backend, no
 * repo. Everything it creates lives under /tmp and is removed at the end.
 *
 * Requires: passwordless ssh to <ssh-host>, and python3 + bash there.
 */
import { spawn } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HOST = process.argv[2] || ''

if (!HOST) {
  console.error('usage: node scripts/ssh-bootstrap-selftest.mjs <ssh-host>')
  process.exit(2)
}

const REPO = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'electron')
const SANDBOX = '/tmp/hermes-bootstrap-selftest'

// Pull REMOTE_BOOTSTRAP_PY out of the TS source without a TS toolchain.
function remotePython() {
  const src = fs.readFileSync(path.join(REPO, 'posix-remote-bootstrap.ts'), 'utf8')
  const start = src.indexOf('const REMOTE_BOOTSTRAP_PY = String.raw`')
  if (start < 0) throw new Error('REMOTE_BOOTSTRAP_PY not found')
  const from = src.indexOf('`', start) + 1
  const end = src.indexOf('`\n', from)
  if (end < 0) throw new Error('unterminated REMOTE_BOOTSTRAP_PY')
  return src.slice(from, end)
}

function shq(v) {
  return `'${String(v).replace(/'/g, `'\\''`)}'`
}

function fingerprint(token) {
  return crypto.createHash('sha256').update(String(token)).digest('hex').slice(0, 32)
}

function ssh(command, { stdinData, timeoutMs = 120000 } = {}) {
  return new Promise((resolve, reject) => {
    const args = [
      '-o', 'BatchMode=yes',
      '-o', 'StrictHostKeyChecking=accept-new',
      '-o', 'ConnectTimeout=15',
      '--', HOST, command
    ]
    const child = spawn('ssh', args, { stdio: [stdinData == null ? 'ignore' : 'pipe', 'pipe', 'pipe'] })
    if (stdinData != null) child.stdin.end(stdinData)
    let out = ''
    let err = ''
    const timer = setTimeout(() => { child.kill('SIGKILL'); reject(new Error(`ssh timed out: ${command.slice(0, 80)}`)) }, timeoutMs)
    child.stdout.on('data', d => { out += d })
    child.stderr.on('data', d => { err += d })
    child.on('close', code => { clearTimeout(timer); resolve({ code, out, err }) })
    child.on('error', e => { clearTimeout(timer); reject(e) })
  })
}

const PY_B64 = Buffer.from(remotePython(), 'utf8').toString('base64')

async function bootstrap(params, token, { home = SANDBOX } = {}) {
  const encoded = Buffer.from(JSON.stringify(params), 'utf8').toString('base64')
  const inner = 'import base64,sys;exec(compile(base64.b64decode(sys.argv[1]),"<hermes-desktop-bootstrap>","exec"))'
  const command = `HOME=${shq(home)} python3 -c ${shq(inner)} ${shq(PY_B64)} ${shq(encoded)}`
  const res = await ssh(command, { stdinData: token })
  const line = res.out.split(/\r?\n/).filter(l => l.startsWith('HERMES_DESKTOP_BOOTSTRAP ')).pop()
  if (!line) throw new Error(`no response line. code=${res.code}\nSTDOUT:${res.out}\nSTDERR:${res.err}`)
  return { command, parsed: JSON.parse(line.slice('HERMES_DESKTOP_BOOTSTRAP '.length)), raw: res }
}

const FAKE_HERMES = [
  '#!/usr/bin/env bash',
  'if [ "$1" = "--version" ]; then echo "Hermes Agent v9.9.9 (fake)"; exit 0; fi',
  'if [ "$1" = "serve" ] && [ "$2" = "--help" ]; then',
  '  echo "  --ssh-session-token-file PATH"; echo "  --ssh-owner-nonce NONCE"; exit 0',
  'fi',
  'sleep 1',
  'echo "HERMES_BACKEND_READY port=45999"',
  'sleep 600'
].join('\n')

const results = []
function check(label, ok, detail = '') {
  results.push({ detail, label, ok })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` -- ${detail}` : ''}`)
}

async function remoteProcCount(nonce) {
  const res = await ssh(`ps -eo pid,cmd | grep -F -- '--ssh-owner-nonce ${nonce}' | grep -v grep | wc -l`)
  return Number(res.out.trim())
}

async function main() {
  const OID = crypto.randomBytes(16).toString('hex')

  // --- sandbox ---
  await ssh(`rm -rf ${shq(SANDBOX)}; mkdir -p ${shq(SANDBOX)}/bin`)
  await ssh(`cat > ${shq(SANDBOX)}/bin/hermes && chmod 755 ${shq(SANDBOX)}/bin/hermes`, { stdinData: FAKE_HERMES })

  const hermesPath = `${SANDBOX}/bin/hermes`
  const baseParams = {
    allowReuse: false,
    nofileSoftLimit: 65536,
    ownershipId: OID,
    profile: '',
    protocolVersion: 1,
    readyTimeoutMs: 30000,
    remoteHermesPath: hermesPath,
    reuseFingerprint: '',
    schemaVersion: 2,
    spawnNonce: '',
    tokenFingerprint: ''
  }

  // --- 1. a bad hermes path fails cleanly and spawns nothing ---
  {
    const token = crypto.randomBytes(32).toString('hex')
    const p = { ...baseParams, remoteHermesPath: `${SANDBOX}/nope`, spawnNonce: crypto.randomBytes(8).toString('hex'), tokenFingerprint: fingerprint(token) }
    const { parsed } = await bootstrap(p, token)
    check('a non-executable hermes path is reported, not guessed around', parsed.ok === false && parsed.kind === 'hermes-not-found', parsed.kind)
  }

  // --- 2. cold spawn ---
  const token1 = crypto.randomBytes(32).toString('hex')
  const nonce1 = crypto.randomBytes(8).toString('hex')
  const first = await bootstrap({ ...baseParams, spawnNonce: nonce1, tokenFingerprint: fingerprint(token1) }, token1)
  check('cold start spawns a backend and reports its announced port', first.parsed.ok === true && first.parsed.port === 45999 && first.parsed.reused === false, JSON.stringify({ pid: first.parsed.pid, port: first.parsed.port, timings: first.parsed.timings }))
  check('the spawn is not reported as a reuse', first.parsed.reused === false)
  check('the version comes from the located binary', first.parsed.hermesVersion === 'Hermes Agent v9.9.9 (fake)', first.parsed.hermesVersion)
  check('a cold start names its reason as no-lock', first.parsed.respawnReason === 'no-lock', first.parsed.respawnReason)

  // token file permissions
  {
    const res = await ssh(`stat -c '%a %U' ${shq(SANDBOX)}/.hermes/desktop-ssh/${OID}/${nonce1}.token ${shq(SANDBOX)}/.hermes/desktop-ssh/${OID}`)
    const lines = res.out.trim().split(/\r?\n/)
    check('the token file is 0600 in a 0700 directory', lines[0]?.startsWith('600 ') && lines[1]?.startsWith('700 '), res.out.trim().replace(/\n/g, ' | '))
  }

  // the token never appears in the remote process list or the command line
  {
    const res = await ssh(`ps -ww -eo cmd | grep -F ${shq(token1)} | grep -v grep | wc -l`)
    check('the token is absent from the remote process list', res.out.trim() === '0', res.out.trim())
    check('the token is absent from the ssh command line', !first.command.includes(token1))
  }

  // --- 3. reuse ---
  {
    const p = { ...baseParams, allowReuse: true, reuseFingerprint: fingerprint(token1), spawnNonce: crypto.randomBytes(8).toString('hex'), tokenFingerprint: fingerprint('unused') }
    const { parsed } = await bootstrap(p, 'unused')
    check('a valid lock is reused instead of spawning again', parsed.ok === true && parsed.reused === true && parsed.pid === first.parsed.pid, JSON.stringify({ pid: parsed.pid, reused: parsed.reused }))
    check('a reuse reports no respawn reason', parsed.respawnReason === '', JSON.stringify(parsed.respawnReason))
    check('reuse costs no second backend', (await remoteProcCount(nonce1)) === 1)
  }

  // --- 3b. reuse explicitly forbidden ---
  {
    const p = { ...baseParams, allowReuse: false, reuseFingerprint: '', spawnNonce: crypto.randomBytes(8).toString('hex'), tokenFingerprint: fingerprint('t') }
    const { parsed } = await bootstrap(p, 't')
    check('forbidding reuse names both blockers', String(parsed.respawnReason).includes('reuse-not-allowed') && String(parsed.respawnReason).includes('no-saved-token'), parsed.respawnReason)
    await ssh(`pkill -f -- '--ssh-owner-nonce ${p.spawnNonce}' 2>/dev/null; true`)
  }

  // --- 4. a wrong reuse fingerprint reaps OUR backend and spawns a new one ---
  const token2 = crypto.randomBytes(32).toString('hex')
  const nonce2 = crypto.randomBytes(8).toString('hex')
  const second = await bootstrap({ ...baseParams, allowReuse: true, reuseFingerprint: fingerprint('a-different-token'), spawnNonce: nonce2, tokenFingerprint: fingerprint(token2) }, token2)
  check('a lock we own but cannot authenticate is respawned', second.parsed.ok === true && second.parsed.reused === false && second.parsed.pid !== first.parsed.pid, JSON.stringify({ newPid: second.parsed.pid, oldPid: first.parsed.pid }))
  check('the respawn names the saved-token mismatch', String(second.parsed.respawnReason).includes('saved-token-mismatch'), second.parsed.respawnReason)
  check('the superseded backend we owned is terminated', (await remoteProcCount(nonce1)) === 0)
  check('exactly one backend remains', (await remoteProcCount(nonce2)) === 1)

  // --- 5. a lock pointing at a FOREIGN pid must never be killed ---
  {
    const victim = await ssh(`nohup sleep 900 >/dev/null 2>&1 & echo $!`)
    const victimPid = Number(victim.out.trim())
    const lockPath = `${SANDBOX}/.hermes/desktop-ssh/${OID}/backend.lock.json`
    // Repoint OUR lock at an unrelated process, keeping every other field valid.
    await ssh(`python3 -c "import json,sys;p=sys.argv[1];d=json.load(open(p));d['pid']=${victimPid};json.dump(d,open(p,'w'))" ${shq(lockPath)}`)
    const token3 = crypto.randomBytes(32).toString('hex')
    const nonce3 = crypto.randomBytes(8).toString('hex')
    const third = await bootstrap({ ...baseParams, allowReuse: true, reuseFingerprint: fingerprint(token2), spawnNonce: nonce3, tokenFingerprint: fingerprint(token3) }, token3)
    const stillAlive = await ssh(`kill -0 ${victimPid} 2>/dev/null && echo ALIVE || echo DEAD`)
    check('a lock pointing at a foreign pid does not kill it', stillAlive.out.trim() === 'ALIVE', stillAlive.out.trim())
    check('an unowned lock still leads to a clean respawn', third.parsed.ok === true && third.parsed.reused === false, JSON.stringify({ kind: third.parsed.kind, reused: third.parsed.reused }))
    check('the respawn names the foreign pid', String(third.parsed.respawnReason).includes('pid-not-ours'), third.parsed.respawnReason)
    await ssh(`kill ${victimPid} 2>/dev/null; true`)
    await ssh(`pkill -f -- '--ssh-owner-nonce ${nonce3}' 2>/dev/null; true`)
  }

  // --- 6. a backend that never announces its port times out and cleans up ---
  {
    const silent = ['#!/usr/bin/env bash',
      'if [ "$1" = "--version" ]; then echo "Hermes Agent v9.9.9 (silent)"; exit 0; fi',
      'if [ "$1" = "serve" ] && [ "$2" = "--help" ]; then echo "--ssh-session-token-file"; echo "--ssh-owner-nonce"; exit 0; fi',
      'sleep 600'].join('\n')
    await ssh(`cat > ${shq(SANDBOX)}/bin/hermes-silent && chmod 755 ${shq(SANDBOX)}/bin/hermes-silent`, { stdinData: silent })
    const OID2 = crypto.randomBytes(16).toString('hex')
    const token4 = crypto.randomBytes(32).toString('hex')
    const nonce4 = crypto.randomBytes(8).toString('hex')
    const started = Date.now()
    const { parsed } = await bootstrap({ ...baseParams, ownershipId: OID2, readyTimeoutMs: 6000, remoteHermesPath: `${SANDBOX}/bin/hermes-silent`, spawnNonce: nonce4, tokenFingerprint: fingerprint(token4) }, token4)
    const elapsed = Date.now() - started
    check('a backend that never announces its port times out with an actionable error', parsed.ok === false && parsed.kind === 'ready-timeout', `${parsed.kind}: ${String(parsed.error).slice(0, 90)}`)
    check('the timeout is bounded by readyTimeoutMs, not the ssh timeout', elapsed < 40000, `${elapsed}ms`)
    check('the timed-out process is reaped', (await remoteProcCount(nonce4)) === 0)
    const leftovers = await ssh(`ls -1 ${shq(SANDBOX)}/.hermes/desktop-ssh/${OID2} 2>/dev/null | wc -l`)
    check('no lock, token or log is left behind after a failed spawn', leftovers.out.trim() === '0', leftovers.out.trim())
  }

  // --- 7. an unsupported/unknown parameter set is refused, not guessed ---
  {
    const { parsed } = await bootstrap({ ...baseParams, ownershipId: 'not-an-id', spawnNonce: 'x' }, 'tok')
    check('a malformed ownership id is refused', parsed.ok === false && parsed.kind === 'unknown', parsed.kind)
  }

  // --- cleanup ---
  await ssh(`pkill -f -- '${SANDBOX}/bin/hermes' 2>/dev/null; pkill -f -- '--ssh-owner-nonce ${nonce2}' 2>/dev/null; true`)
  await ssh(`rm -rf ${shq(SANDBOX)}`)
  const strays = await ssh(`ps -eo cmd | grep -F -- 'hermes-bootstrap-selftest' | grep -v grep | wc -l`)
  check('the sandbox leaves no process behind', strays.out.trim() === '0', strays.out.trim())

  const failed = results.filter(r => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
  if (failed.length) {
    process.exitCode = 1
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(error => {
    console.error('SELFTEST ERROR:', error)
    process.exitCode = 1
  })
}
