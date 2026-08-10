/**
 * remote-lifecycle.ts
 *
 * Pure, electron-free remote Hermes dashboard lifecycle over SSH for Desktop
 * SSH remote mode. Composes an SshConnection (injected) with HTTP probes
 * through the established tunnel (injected fetch) and the served-token adoption
 * step (injected). Knows how to:
 *
 *   - locate the Hermes install on the remote (login-shell probe),
 *   - gate the remote platform to Linux via `uname` (Darwin/macOS remote
 *     backends are unsupported because process replacement lacks an
 *     identity-stable non-pidfd signal primitive),
 *   - reuse an existing desktop-dedicated dashboard via a lockfile + an
 *     AUTHENTICATED /api/status probe (pid liveness alone is insufficient),
 *   - spawn a fresh detached `--isolated --port 0` dashboard and scrape its
 *     readiness line,
 *   - adopt the token the dashboard actually serves (served-token adoption),
 *   - clean up a stale dashboard only when it is provably ours,
 *   - fail closed on alive indeterminate/foreign locks (no delete/signal/spawn),
 *   - serialize the full remote lifecycle transaction with a per-ownership mutex.
 *
 * Lock schema v3 persists spawn-observed process identity (pid, kernel start
 * time, argv fingerprint). The launcher path is metadata only after exec
 * wrappers — ownership never requires argv[0]/argv[1] to equal the launcher.
 *
 * No `import 'electron'` so it's unit-testable with `node --test`. main.ts wires
 * the real SshConnection, fetch, adoptServedDashboardToken, and waitForHermes in.
 */

import crypto from 'node:crypto'

const LOCKFILE_SCHEMA_VERSION = 3
const SUPPORTED_LOCK_SCHEMA_VERSIONS = new Set([2, 3])
// Bumped when the desktop<->dashboard reuse contract changes in a way that makes
// an old running dashboard unsafe to reattach to (token handling, readiness/spawn
// args, served-token reconciliation). A mismatch forces a clean respawn.
const PROTOCOL_VERSION = 1
const READY_RE = /^HERMES_(?:BACKEND|DASHBOARD)_READY port=(\d+)/m
const REMOTE_LOCK_DIR = '~/.hermes/desktop-ssh'
// Linux only: remote replacement requires pidfd-bound signaling so a reused PID
// cannot receive SIGTERM. Darwin has no identity-stable non-pidfd primitive we
// accept for replacement, so macOS remotes fail closed at platform probe.
const SUPPORTED_REMOTE_OS = new Set(['Linux'])
const DEFAULT_READY_TIMEOUT_MS = 45_000
const READY_POLL_INTERVAL_MS = 750
// macOS sshd starts non-interactive shells with a 256-FD soft limit even when
// the hard limit is unlimited. A Desktop backend can legitimately exceed that
// while serving several profiles/tools, so raise only the child process limit.
// Keep startup portable: restricted hosts retain their existing limit.
const REMOTE_NOFILE_SOFT_LIMIT = 65_536
const OWNERSHIP_MUTEX_STALE_MS = 120_000
const OWNERSHIP_MUTEX_HEARTBEAT_MS = 30_000
const LOCK_ABSENT_MARKER = '__HERMES_DESKTOP_LOCK_ABSENT__'
const LOCK_PRESENT_MARKER = '__HERMES_DESKTOP_LOCK_PRESENT__'

function mintToken() {
  return crypto.randomBytes(32).toString('hex')
}

// Fingerprint a token for the lockfile — never store the raw secret on the
// remote. SHA256, truncated.
function fingerprintToken(token) {
  return crypto
    .createHash('sha256')
    .update(String(token || ''))
    .digest('hex')
    .slice(0, 32)
}

function validateOwnershipId(ownershipId) {
  const value = String(ownershipId || '')

  if (!/^[0-9a-f]{32}$/.test(value)) {
    throw new Error('SSH ownership ID is invalid.')
  }

  return value
}

function validateSpawnNonce(spawnNonce) {
  const value = String(spawnNonce || '')

  if (!/^[0-9a-f]{16}$/.test(value)) {
    throw new Error('SSH spawn nonce is invalid.')
  }

  return value
}

function ownershipDirectory(ownershipId) {
  return `${REMOTE_LOCK_DIR}/${validateOwnershipId(ownershipId)}`
}

function lockfilePath(ownershipId) {
  return `${ownershipDirectory(ownershipId)}/backend.lock.json`
}

function ownershipMutexPath(ownershipId) {
  return `${ownershipDirectory(ownershipId)}/lifecycle.mutex`
}

function spawnLogPath(ownershipId, spawnNonce) {
  return `${ownershipDirectory(ownershipId)}/${validateSpawnNonce(spawnNonce)}.log`
}

// shell-single-quote a value for safe interpolation into a remote command.
function shq(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`
}

function validateRemotePath(p) {
  const s = String(p || '')

  if (!s) {
    throw new Error('Remote path must not be empty.')
  }

  // eslint-disable-next-line no-control-regex -- deliberately reject NUL in remote paths
  if (/[\x00\n\r]/.test(s)) {
    throw new Error('Unsafe remote path: contains NUL or newline.')
  }

  if (s === '~' || s.startsWith('~/') || s.startsWith('/')) {
    return
  }

  throw new Error(`Remote path must be absolute or start with ~/: "${s}"`)
}

function expandRemotePath(p) {
  validateRemotePath(p)

  if (p === '~') {
    return '"$HOME"'
  }

  if (p.startsWith('~/')) {
    return '"$HOME"' + shq(p.slice(1))
  }

  return shq(p)
}

function ownershipConflict(message) {
  const error: any = new Error(message || 'SSH backend ownership conflict.')
  error.kind = 'ownership-conflict'

  return error
}

// Resolve the remote hermes executable. An EXPLICIT path is honored strictly
// (throws a path-naming error if not executable — never silently falls back to a
// different install). A BLANK path auto-detects: login-shell `command -v` (a
// non-login `ssh host cmd` PATH misses user installs), then known install paths.
async function locateHermes(ssh, remoteHermesPath) {
  const resolveLauncher = async (candidate: string) => {
    // Return the candidate path directly. The hermes binary or wrapper script
    // is executable and handles argument forwarding (e.g. `exec <python> <script> "$@"`)
    // correctly on its own. Previously, this function followed `exec` wrappers and
    // returned only the python interpreter, which broke:
    //   - version checking: `<python> --version` printed "Python x.y.z" instead of
    //     the Hermes version, and
    //   - capability probing: `<python> serve --help` failed entirely.
    // See https://github.com/NousResearch/hermes-agent/issues/74411
    return candidate
  }

  const isExecutable = async (candidate: string) => {
    try {
      validateRemotePath(candidate)
      const ok = (await ssh.exec(`[ -x ${expandRemotePath(candidate)} ] && echo OK || true`)).trim()

      return ok === 'OK'
    } catch {
      return false
    }
  }

  if (remoteHermesPath) {
    if (await isExecutable(remoteHermesPath)) {
      return resolveLauncher(remoteHermesPath)
    }

    const err: any = new Error(
      `The Hermes path you set is not an executable on the remote host: "${remoteHermesPath}". ` +
        'Check the path (it must be the full path to the `hermes` binary on the remote, e.g. ' +
        '~/hermes-agent/.venv/bin/hermes), or clear it to auto-detect.'
    )

    err.kind = 'hermes-not-found'
    throw err
  }

  const candidates: string[] = []

  try {
    const found = (await ssh.exec(`bash -lc ${shq('command -v hermes')}`)).trim()

    if (found) {
      candidates.push(found.split('\n').pop().trim())
    }
  } catch {
    // ignore
  }

  // Fallback candidates when the login-shell probe misses: the installer's
  // command locations (scripts/install.sh) — per-user, root/FHS, legacy venv.
  candidates.push('~/.local/bin/hermes')
  candidates.push('/usr/local/bin/hermes')
  candidates.push('~/.hermes/hermes-agent/venv/bin/hermes')

  for (const candidate of candidates) {
    if (!candidate) {
      continue
    }

    if (await isExecutable(candidate)) {
      return resolveLauncher(candidate)
    }
  }

  const err: any = new Error(
    'Hermes is not installed on the remote host (could not find a `hermes` executable). ' +
      'Install it on the remote with:  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh  ' +
      '— or set the Hermes path explicitly in the SSH connection settings.'
  )

  err.kind = 'hermes-not-found'
  throw err
}

// Probe the resolved binary's version string (first line of `<hermes> --version`,
// e.g. "Hermes Agent v0.18.2 ..."), or '' on failure. Surfaces WHICH hermes a
// connection uses, so a stale/unexpected install is visible.
async function probeHermesVersion(ssh, hermesPath) {
  try {
    const out = (await ssh.exec(`${expandRemotePath(hermesPath)} --version 2>&1`)).trim()

    return (out.split('\n')[0] || '').trim()
  } catch {
    return ''
  }
}

async function probeRemotePlatform(ssh) {
  const out = (await ssh.exec('uname -s; uname -m')).trim().split('\n')
  const osName = (out[0] || '').trim()
  const arch = (out[1] || '').trim()

  if (!SUPPORTED_REMOTE_OS.has(osName)) {
    const err: any = new Error(
      `Unsupported remote platform "${osName || 'unknown'}". Hermes Desktop SSH backend replacement supports Linux only ` +
        '(identity-stable pidfd termination). Darwin/macOS remotes are not supported for replacement because a ' +
        'prove-then-kill path can signal a reused PID.'
    )

    err.kind = 'unsupported-platform'
    throw err
  }

  return { os: osName, arch }
}

// The HERMES_HOME the remote dashboard will use (explicit env wins, else
// ~/.hermes). Recorded in the lockfile so a future reuse can tell it's the same
// state store; best-effort.
async function probeRemoteHermesHome(ssh) {
  try {
    const out = (await ssh.exec('echo "${HERMES_HOME:-$HOME/.hermes}"')).trim().split('\n').pop()

    return out || '~/.hermes'
  } catch (cause) {
    const error: any = new Error('Could not resolve the remote Hermes home.')
    error.kind = 'transient-transport-error'
    error.cause = cause
    throw error
  }
}

function normalizeLockRecord(parsed, ownershipId) {
  if (!parsed || !SUPPORTED_LOCK_SCHEMA_VERSIONS.has(parsed.schemaVersion)) {
    return null
  }

  const pid = parsed.pid
  const port = parsed.port

  if (!Number.isInteger(pid) || pid <= 0 || pid > 4194304) {
    return null
  }

  // port 0 = spawn-in-progress record (written before readiness); valid
  // ownership proof for cleanup, but never reusable.
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    return null
  }

  if (parsed.ownershipId !== ownershipId || !/^[0-9a-f]{16}$/.test(parsed.spawnNonce || '')) {
    return null
  }

  if (!/^[0-9a-f]{32}$/.test(parsed.tokenFingerprint || '')) {
    return null
  }

  if (parsed.protocolVersion !== PROTOCOL_VERSION) {
    return null
  }

  if (parsed.logPath !== spawnLogPath(ownershipId, parsed.spawnNonce)) {
    return null
  }

  for (const field of ['profile', 'hermesPath', 'hermesHome', 'logPath', 'startedAt']) {
    if (typeof parsed[field] !== 'string' || parsed[field].length > 1024) {
      return null
    }
  }

  // launcherPath is optional metadata (v3); fall back to hermesPath.
  if (parsed.launcherPath != null) {
    if (typeof parsed.launcherPath !== 'string' || parsed.launcherPath.length > 1024) {
      return null
    }
  } else {
    parsed.launcherPath = parsed.hermesPath
  }

  if (parsed.pidStartTime != null) {
    if (!Number.isInteger(parsed.pidStartTime) || parsed.pidStartTime < 0) {
      return null
    }
  }

  if (parsed.processFingerprint != null) {
    if (typeof parsed.processFingerprint !== 'string' || !/^[0-9a-f]{16,128}$/.test(parsed.processFingerprint)) {
      return null
    }
  }

  return parsed
}

async function readOwnershipRecord(ssh, ownershipId) {
  const lpath = lockfilePath(ownershipId)
  let raw

  try {
    raw = await ssh.exec(
      `if [ -e ${expandRemotePath(lpath)} ]; then ` +
        `printf '%s\\n' ${shq(LOCK_PRESENT_MARKER)}; cat ${expandRemotePath(lpath)}; ` +
        `else printf '%s\\n' ${shq(LOCK_ABSENT_MARKER)}; fi`
    )
  } catch (cause) {
    const error: any = new Error('Could not read the SSH backend ownership record.')
    error.kind = 'transient-transport-error'
    error.cause = cause
    throw error
  }

  let text = String(raw || '').trim()

  // Empty unmarked output is retained as a test-double/back-compat spelling of
  // absence. Real remote reads always emit an explicit PRESENT/ABSENT marker,
  // so an existing empty file cannot be confused with no file.
  if (!text || text === LOCK_ABSENT_MARKER) {
    return { state: 'absent', record: null, parsed: null }
  }

  if (text.startsWith(`${LOCK_PRESENT_MARKER}\n`)) {
    text = text.slice(LOCK_PRESENT_MARKER.length + 1).trim()
  } else if (text === LOCK_PRESENT_MARKER) {
    text = ''
  }

  let parsed

  try {
    parsed = JSON.parse(text)
  } catch {
    return { state: 'invalid', record: null, parsed: null }
  }

  const record = normalizeLockRecord(parsed, ownershipId)

  return record
    ? { state: 'valid', record, parsed }
    : { state: 'invalid', record: null, parsed }
}

async function readLockfile(ssh, ownershipId) {
  const envelope = await readOwnershipRecord(ssh, ownershipId)

  return envelope.state === 'valid' ? envelope.record : null
}

async function writeLockfile(ssh, ownershipId, lock, lease: any = null) {
  const directory = ownershipDirectory(ownershipId)
  const lpath = lockfilePath(ownershipId)
  const temporaryPath = `${directory}/.${crypto.randomBytes(8).toString('hex')}.lock.tmp`
  const json = JSON.stringify({ ...lock, schemaVersion: LOCKFILE_SCHEMA_VERSION })

  const body =
    'import os\n' +
    `d=os.path.expanduser(${shq(directory)})\n` +
    `tmp=os.path.expanduser(${shq(temporaryPath)})\n` +
    `dst=os.path.expanduser(${shq(lpath)})\n` +
    `payload=${shq(json)}\n` +
    'os.makedirs(d,mode=0o700,exist_ok=True)\n' +
    'fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)\n' +
    'try:os.write(fd,payload.encode())\n' +
    'finally:os.close(fd)\n' +
    'os.replace(tmp,dst)\n' +
    'print("WROTE")'

  if (lease) {
    const out = String(await lease.runHeld(body)).trim()

    if (!out.endsWith('WROTE')) {
      const error: any = new Error('Could not write the SSH backend ownership record under the lifecycle lease.')
      error.kind = 'transient-transport-error'
      throw error
    }

    return
  }

  await ssh.exec(`python3 -c ${shq(body)}`)
}

async function removeLockfile(ssh, ownershipId, lease: any = null) {
  const lpath = lockfilePath(ownershipId)

  const body =
    'import os\n' +
    `p=os.path.expanduser(${shq(lpath)})\n` +
    'try:os.unlink(p)\n' +
    'except FileNotFoundError:pass\n' +
    'print("REMOVED")'

  try {
    if (lease) {
      await lease.runHeld(body)

      return
    }

    await ssh.exec(`python3 -c ${shq(body)}`)
  } catch (error) {
    if (lease) {
      throw error
    }
  }
}

async function remotePidAlive(ssh, pid) {
  if (!pid || !Number.isInteger(Number(pid))) {
    return false
  }

  try {
    const out = (await ssh.exec(`kill -0 ${Number(pid)} 2>/dev/null && echo ALIVE || echo DEAD`)).trim()

    return out === 'ALIVE'
  } catch (cause) {
    const error: any = new Error('Could not verify the SSH backend process.')
    error.kind = 'transient-transport-error'
    error.cause = cause
    throw error
  }
}

/**
 * Observe the live process identity for a remote PID.
 * Returns { startTime, fingerprint, ownedShape } or null when the PID is gone.
 * Transport failures raise transient-transport-error.
 */
async function observeProcessIdentity(ssh, pid, spawnNonce) {
  if (!pid || !/^[0-9a-f]{16}$/.test(String(spawnNonce || ''))) {
    return null
  }

  try {
    const script =
      'import hashlib,os,shlex,subprocess,sys\n' +
      `pid=${Number(pid)}\n` +
      `nonce=${shq(spawnNonce)}\n` +
      'def read_args(p):\n' +
      ' try:\n' +
      '  raw=open(f"/proc/{p}/cmdline","rb").read()\n' +
      '  return [x.decode("utf-8","surrogateescape") for x in raw.split(b"\\0") if x]\n' +
      ' except OSError:\n' +
      '  line=subprocess.check_output(["ps","-o","command=","-p",str(p)],text=True).strip()\n' +
      '  return shlex.split(line)\n' +
      'def start_time(p):\n' +
      ' try:\n' +
      '  return int(open(f"/proc/{p}/stat","r",encoding="utf-8").read().split()[21])\n' +
      ' except Exception:\n' +
      '  try:\n' +
      '   out=subprocess.check_output(["ps","-o","lstart=","-p",str(p)],text=True).strip()\n' +
      '   return int(hashlib.sha256(out.encode()).hexdigest()[:15],16)\n' +
      '  except Exception:\n' +
      '   return None\n' +
      'try:\n' +
      ' args=read_args(pid)\n' +
      'except Exception:\n' +
      ' try:os.kill(pid,0)\n' +
      ' except ProcessLookupError:print("GONE"); sys.exit(0)\n' +
      ' except PermissionError:pass\n' +
      ' print("INDETERMINATE"); sys.exit(0)\n' +
      'st=start_time(pid)\n' +
      'fp=hashlib.sha256("\\0".join(args).encode("utf-8","surrogateescape")).hexdigest()[:32]\n' +
      'ok=False\n' +
      'try:\n' +
      ' serve=args.index("serve")\n' +
      ' owner=args.index("--ssh-owner-nonce",serve+1)\n' +
      ' ok=("--isolated" in args[serve+1:]) and args[owner+1]==nonce\n' +
      'except (ValueError,IndexError):\n' +
      ' ok=False\n' +
      'print("OK" if ok else "SHAPE")\n' +
      'print(st if st is not None else "")\n' +
      'print(fp)\n' +
      'print("\\0".join(args))'

    const out = String(await ssh.exec(`python3 -c ${shq(script)}`)).trim()
    const lines = out.split('\n')
    const status = (lines[0] || '').trim()

    if (status === 'GONE' || !status) {
      return null
    }

    const startRaw = (lines[1] || '').trim()
    const fingerprint = (lines[2] || '').trim()
    const startTime = startRaw === '' ? null : Number(startRaw)

    return {
      startTime: Number.isInteger(startTime) ? startTime : null,
      fingerprint: /^[0-9a-f]{16,128}$/.test(fingerprint) ? fingerprint : '',
      ownedShape: status === 'OK'
    }
  } catch (cause) {
    const error: any = new Error('Could not observe SSH backend process identity.')
    error.kind = 'transient-transport-error'
    error.cause = cause
    throw error
  }
}

/**
 * A pid is "provably ours" only when the live process carries the exact serve
 * ownership shape and matches any spawn-observed identity recorded in the lock.
 * The launcher path is metadata after exec wrappers — never an ownership
 * invariant.
 *
 * Returns:
 *   'owned'          — positively identified as our dashboard
 *   'foreign'        — positively identified as not ours / PID reuse
 *   'gone'           — the recorded PID no longer exists
 *   'indeterminate'  — cannot prove either way (should fail closed if alive)
 */
async function classifyProcessOwnership(ssh, lock) {
  if (!lock || !lock.pid || !/^[0-9a-f]{16}$/.test(String(lock.spawnNonce || ''))) {
    return 'indeterminate'
  }

  const observed = await observeProcessIdentity(ssh, lock.pid, lock.spawnNonce)

  if (!observed) {
    return 'gone'
  }

  if (!observed.ownedShape) {
    return 'foreign'
  }

  if (lock.pidStartTime != null && observed.startTime != null && lock.pidStartTime !== observed.startTime) {
    // Same PID number, different start time => PID reuse. Never signal it.
    return 'foreign'
  }

  if (
    lock.processFingerprint &&
    observed.fingerprint &&
    lock.processFingerprint !== observed.fingerprint
  ) {
    return 'foreign'
  }

  return 'owned'
}

// Back-compat predicate used by tests and callers that want a boolean.
async function pidIsOurDashboard(ssh, pid, spawnNonce, _hermesPath = '', lockExtras: any = {}) {
  const classification = await classifyProcessOwnership(ssh, {
    pid,
    spawnNonce,
    pidStartTime: lockExtras.pidStartTime,
    processFingerprint: lockExtras.processFingerprint
  })

  return classification === 'owned'
}

/**
 * Re-prove identity and signal through a pidfd in one remote helper. The pidfd
 * binds the signal target before the final identity check, so PID reuse between
 * proof and signal cannot redirect SIGTERM to a replacement process. Platforms
 * without pidfds fail closed with UNSUPPORTED — never prove-then-os.kill.
 */
async function terminateOwnedProcess(ssh, lock, lease: any = null) {
  const expectedStart = lock.pidStartTime == null ? 'None' : String(Number(lock.pidStartTime))
  const expectedFingerprint = String(lock.processFingerprint || '')
  const leasePath = lease?.path || ''
  const leaseToken = lease?.token || lease?.holder || ''

  const script =
    'import hashlib,os,signal,sys,shlex,subprocess\n' +
    'try:\n' +
    ' import fcntl\n' +
    'except ImportError:\n' +
    ' fcntl=None\n' +
    `pid=${Number(lock.pid)}\n` +
    `nonce=${shq(lock.spawnNonce)}\n` +
    `expected_start=${expectedStart}\n` +
    `expected_fingerprint=${shq(expectedFingerprint)}\n` +
    `lease_path=${shq(leasePath)}\n` +
    `lease_token=${shq(leaseToken)}\n` +
    'terminate_result="MISMATCH"\n' +
    'def finish(value):\n' +
    ' print(value); sys.exit(0)\n' +
    'def fence_lease():\n' +
    ' if not lease_path:\n' +
    '  return None\n' +
    ' p=os.path.expanduser(lease_path)\n' +
    ' try:\n' +
    '  fd=os.open(p,os.O_RDWR)\n' +
    ' except OSError:\n' +
    '  finish("LOST")\n' +
    ' try:\n' +
    '  if fcntl is not None:\n' +
    '   fcntl.flock(fd,fcntl.LOCK_EX)\n' +
    '  data=os.read(fd,4096).decode().strip()\n' +
    '  if data!=lease_token:\n' +
    '   finish("LOST")\n' +
    '  try:os.utime(p,None)\n' +
    '  except OSError:pass\n' +
    '  return fd\n' +
    ' except Exception:\n' +
    '  try:os.close(fd)\n' +
    '  except OSError:pass\n' +
    '  raise\n' +
    'def read_args(p):\n' +
    ' try:\n' +
    '  raw=open(f"/proc/{p}/cmdline","rb").read()\n' +
    '  return [x.decode("utf-8","surrogateescape") for x in raw.split(b"\\0") if x], "proc"\n' +
    ' except OSError:\n' +
    '  line=subprocess.check_output(["ps","-o","command=","-p",str(p)],text=True).strip()\n' +
    '  return shlex.split(line), "ps"\n' +
    'def start_time(p):\n' +
    ' try:\n' +
    '  return int(open(f"/proc/{p}/stat","r",encoding="utf-8").read().split()[21]), "proc"\n' +
    ' except Exception:\n' +
    '  out=subprocess.check_output(["ps","-o","lstart=","-p",str(p)],text=True).strip()\n' +
    '  return int(hashlib.sha256(out.encode()).hexdigest()[:15],16), "ps"\n' +
    'def prove_identity():\n' +
    ' try:\n' +
    '  args, _src=read_args(pid)\n' +
    ' except Exception:\n' +
    '  try:os.kill(pid,0)\n' +
    '  except ProcessLookupError:finish("GONE")\n' +
    '  except PermissionError:finish("UNSAFE")\n' +
    '  finish("UNSAFE")\n' +
    ' try:\n' +
    '  start,_=start_time(pid)\n' +
    ' except Exception:\n' +
    '  finish("GONE")\n' +
    ' fp=hashlib.sha256("\\0".join(args).encode("utf-8","surrogateescape")).hexdigest()[:32]\n' +
    ' try:\n' +
    '  serve=args.index("serve"); owner=args.index("--ssh-owner-nonce",serve+1)\n' +
    '  shape=("--isolated" in args[serve+1:]) and args[owner+1]==nonce\n' +
    ' except (ValueError,IndexError):\n' +
    '  shape=False\n' +
    ' if not shape:finish("MISMATCH")\n' +
    ' if expected_start is not None and start!=expected_start:finish("MISMATCH")\n' +
    ' if expected_fingerprint and fp!=expected_fingerprint:finish("MISMATCH")\n' +
    ' return args,start,fp\n' +
    'lease_fd=fence_lease()\n' +
    'try:\n' +
    ' try:\n' +
    '  pidfd=os.pidfd_open(pid)\n' +
    ' except ProcessLookupError:\n' +
    '  finish("GONE")\n' +
    ' except (AttributeError,NotImplementedError,OSError):\n' +
    '  # No identity-stable non-pidfd signal primitive is accepted. Refuse to\n' +
    '  # prove_identity()+os.kill (PID-reuse race). Darwin is unsupported.\n' +
    '  finish("UNSUPPORTED")\n' +
    ' try:\n' +
    '  prove_identity()\n' +
    '  try:signal.pidfd_send_signal(pidfd,signal.SIGTERM)\n' +
    '  except ProcessLookupError:finish("GONE")\n' +
    '  finish("TERMINATED")\n' +
    ' finally:\n' +
    '  try:os.close(pidfd)\n' +
    '  except Exception:pass\n' +
    'finally:\n' +
    ' if lease_fd is not None:\n' +
    '  try:os.close(lease_fd)\n' +
    '  except OSError:pass'

  try {
    return String(await ssh.exec(`python3 -c ${shq(script)}`)).trim()
  } catch (cause) {
    const error: any = new Error('Could not terminate the stale SSH backend.')
    error.kind = 'transient-transport-error'
    error.cause = cause
    throw error
  }
}

// Kill the stale dashboard ONLY if provably ours, then drop the lockfile.
// Alive indeterminate/foreign locks are a hard ownership-conflict: preserve
// lock/log, do not signal, do not spawn.
async function cleanupStale(ssh, ownershipId, lock, pidAlive = true, lease: any = null) {
  const lockWithOwner = lock ? { ...lock, ownershipId: lock.ownershipId || ownershipId } : lock

  if (pidAlive && lockWithOwner) {
    const classification = await classifyProcessOwnership(ssh, lockWithOwner)

    if (classification === 'owned') {
      const terminateResult = await terminateOwnedProcess(ssh, lockWithOwner, lease)

      if (terminateResult === 'MISMATCH' || terminateResult === 'UNSAFE' || terminateResult === 'LOST') {
        throw ownershipConflict(
          'SSH backend identity or lifecycle lease changed before termination; preserving lock evidence.'
        )
      }

      if (terminateResult === 'UNSUPPORTED') {
        const error: any = new Error(
          'SSH backend replacement requires identity-stable pidfd termination; this remote platform is unsupported.'
        )
        error.kind = 'unsupported-platform'
        throw error
      }

      if (!['TERMINATED', 'GONE'].includes(terminateResult)) {
        const error: any = new Error('SSH backend termination returned an invalid result.')
        error.kind = 'transient-transport-error'
        throw error
      }

      // SIGTERM is asynchronous. Wait boundedly for the exact PID number to
      // disappear before dropping evidence or allowing a replacement spawn.
      // If the PID is reused during this wait it remains ALIVE and we fail
      // closed; we never signal the replacement.
      if (terminateResult === 'TERMINATED') {
        const deadline = Date.now() + 5_000
        let stillAlive = await remotePidAlive(ssh, lockWithOwner.pid)

        while (stillAlive && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 100))
          stillAlive = await remotePidAlive(ssh, lockWithOwner.pid)
        }

        if (stillAlive) {
          throw ownershipConflict(
            'Owned SSH backend did not exit after terminate; preserving lock evidence.'
          )
        }
      }
    } else if (classification === 'foreign' || classification === 'indeterminate') {
      throw ownershipConflict(
        'Alive SSH backend lock is not provably owned; refusing to delete lock/log or spawn a replacement.'
      )
    }
  }

  const expectedLogPath =
    lockWithOwner?.spawnNonce ? spawnLogPath(ownershipId, lockWithOwner.spawnNonce) : ''

  if (lockWithOwner?.logPath && lockWithOwner.logPath === expectedLogPath) {
    const logBody =
      'import os\n' +
      `p=os.path.expanduser(${shq(lockWithOwner.logPath)})\n` +
      'try:os.unlink(p)\n' +
      'except FileNotFoundError:pass\n' +
      'print("LOG_REMOVED")'

    try {
      if (lease) {
        await lease.runHeld(logBody)
      } else {
        await ssh.exec(`python3 -c ${shq(logBody)}`)
      }
    } catch {
      void 0
    }
  }

  await removeLockfile(ssh, ownershipId, lease)
}

// Detach so the backend survives the SSH channel closing: setsid (Linux)
// starts a new session; macOS has no setsid, so fall back to nohup (HUP-immune;
// fd-detachment is already handled by </dev/null + redirect + &).
function buildSpawnCommand(hermesPath, profile, opts: any = {}) {
  const hermes = expandRemotePath(hermesPath)
  const profileArgs = profile ? `--profile ${shq(profile)} ` : ''
  const logPath = expandRemotePath(opts.logPath)
  const tokenFilePath = opts.tokenFilePath
  const tokenArg = tokenFilePath ? ` --ssh-session-token-file ${expandRemotePath(tokenFilePath)}` : ''
  const ownerArg = opts.spawnNonce ? ` --ssh-owner-nonce ${validateSpawnNonce(opts.spawnNonce)}` : ''
  const subCmd = `serve --isolated --host 127.0.0.1 --port 0${tokenArg}${ownerArg}`

  const dashCmd =
    `ulimit -n ${REMOTE_NOFILE_SOFT_LIMIT} 2>/dev/null || true; ` +
    `exec env HERMES_DESKTOP=1 ${hermes} ${profileArgs}${subCmd}`

  return (
    `mkdir -p "$(dirname ${logPath})" && ` +
    `"$(command -v setsid || echo nohup)" sh -c ${shq(`${dashCmd} </dev/null >> ${logPath} 2>&1 & echo $!`)}`
  )
}

async function remoteSupportsSshOwnership(ssh, hermesPath) {
  const hermes = expandRemotePath(hermesPath)

  const out = await ssh.exec(
    `help="$(${hermes} serve --help 2>&1)"; ` +
      `printf '%s' "$help" | grep -q ssh-session-token-file && ` +
      `printf '%s' "$help" | grep -q ssh-owner-nonce && echo YES || echo NO`
  )

  return String(out || '')
    .trim()
    .endsWith('YES')
}

async function scrapeReadyPort(ssh, logPath, { timeoutMs = DEFAULT_READY_TIMEOUT_MS, isAlive, signal }: any = {}) {
  const deadline = Date.now() + timeoutMs
  const remoteLog = expandRemotePath(logPath)

  while (Date.now() < deadline) {
    assertNotAborted(signal)

    if (isAlive && !(await isAlive())) {
      const err: any = new Error('Remote dashboard process exited before announcing its port.')
      err.kind = 'spawn-failed'
      throw err
    }

    let tail

    try {
      tail = await ssh.exec(`cat ${remoteLog} 2>/dev/null || true`)
    } catch {
      tail = ''
    }

    const m = READY_RE.exec(String(tail || ''))

    if (m) {
      return parseInt(m[1], 10)
    }

    await new Promise(r => setTimeout(r, READY_POLL_INTERVAL_MS))
  }

  const err: any = new Error(`Timed out waiting for the remote dashboard to announce its port (${timeoutMs}ms).`)
  err.kind = 'ready-timeout'
  throw err
}

function indentPython(source, levels = 1) {
  const pad = ' '.repeat(levels)

  return String(source || '')
    .split('\n')
    .map(line => (line.length ? pad + line : line))
    .join('\n')
}

/**
 * Python preamble that opens the lifecycle mutex, takes an exclusive flock when
 * available, and verifies the owner-epoch token before any mutation body runs.
 * Leaves `lease_fd` open for the caller to close.
 */
function buildLeaseFenceOpen(lease) {
  return (
    'try:\n' +
    ' import fcntl\n' +
    'except ImportError:\n' +
    ' fcntl=None\n' +
    `p=os.path.expanduser(${shq(lease.path)})\n` +
    `lease_token=${shq(lease.token)}\n` +
    'try:\n' +
    ' lease_fd=os.open(p,os.O_RDWR)\n' +
    'except OSError:\n' +
    ' print("LOST"); raise SystemExit(0)\n' +
    'if fcntl is not None:\n' +
    ' fcntl.flock(lease_fd,fcntl.LOCK_EX)\n' +
    'data=os.read(lease_fd,4096).decode().strip()\n' +
    'if data!=lease_token:\n' +
    ' os.close(lease_fd)\n' +
    ' print("LOST"); raise SystemExit(0)\n' +
    'try:os.utime(p,None)\n' +
    'except OSError:pass\n'
  )
}

async function spawnRemoteDashboard(ssh, { hermesPath, profile, token, ownershipId, lease = null }) {
  if (!(await remoteSupportsSshOwnership(ssh, hermesPath))) {
    const err: any = new Error(
      'The remote Hermes install does not support --ssh-session-token-file and --ssh-owner-nonce. ' +
        'Update Hermes on the remote host to continue using Desktop SSH mode.'
    )

    err.kind = 'update-required'
    throw err
  }

  const spawnNonce = crypto.randomBytes(8).toString('hex')
  const tokenDir = ownershipDirectory(ownershipId)
  const tokenFilePath = `${tokenDir}/${spawnNonce}.token`
  const logPath = spawnLogPath(ownershipId, spawnNonce)

  const tokenUploadPy =
    'import os,sys,stat\n' +
    `p=os.path.expanduser(${shq(tokenFilePath)})\n` +
    'd=os.path.dirname(p)\n' +
    'n=os.path.basename(p)\n' +
    'os.makedirs(d,mode=0o700,exist_ok=True)\n' +
    'df=os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_NOFOLLOW",0)\n' +
    'dd=os.open(d,df)\n' +
    'try:\n' +
    ' s=os.fstat(dd)\n' +
    ' if not stat.S_ISDIR(s.st_mode):raise SystemExit("unsafe token directory")\n' +
    ' if hasattr(os,"getuid") and s.st_uid!=os.getuid():raise SystemExit("token directory owner mismatch")\n' +
    ' if (s.st_mode&0o777)!=0o700:os.fchmod(dd,0o700)\n' +
    ' fl=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0)\n' +
    ' now=__import__("time").time()\n' +
    ' for stale in os.listdir(dd):\n' +
    '  if stale.endswith(".token") and len(stale)==22:\n' +
    '   try:\n' +
    '    ss=os.stat(stale,dir_fd=dd,follow_symlinks=False)\n' +
    '    if stat.S_ISREG(ss.st_mode) and now-ss.st_mtime>3600:os.unlink(stale,dir_fd=dd)\n' +
    '   except OSError:pass\n' +
    ' fd=os.open(n,fl,0o600,dir_fd=dd)\n' +
    ' try:os.write(fd,sys.stdin.buffer.read())\n' +
    ' except BaseException:\n' +
    '  try:os.unlink(n,dir_fd=dd)\n' +
    '  except OSError:pass\n' +
    '  raise\n' +
    ' finally:os.close(fd)\n' +
    'finally:os.close(dd)'

  const cleanupToken = async () => {
    const body =
      'import os\n' +
      `p=os.path.expanduser(${shq(tokenFilePath)})\n` +
      'try:os.unlink(p)\n' +
      'except FileNotFoundError:pass\n' +
      'print("TOKEN_REMOVED")'

    try {
      if (lease) {
        await lease.runHeld(body)
      } else {
        await ssh.exec(`python3 -c ${shq(body)}`)
      }
    } catch {
      void 0
    }
  }

  try {
    if (lease) {
      // Token bytes still stream over stdin; fence the filesystem mutation only.
      const fencedUpload =
        'import os,sys\n' +
        buildLeaseFenceOpen(lease) +
        'try:\n' +
        indentPython(tokenUploadPy, 1) +
        '\n finally:\n' +
        '  os.close(lease_fd)\n'

      await ssh.exec(`python3 -c ${shq(fencedUpload)}`, { stdinData: token })
    } else {
      await ssh.exec(`python3 -c ${shq(tokenUploadPy)}`, { stdinData: token })
    }
  } catch (error) {
    await cleanupToken()
    throw error
  }

  let out

  try {
    const spawnCmd = buildSpawnCommand(hermesPath, profile, { spawnNonce, tokenFilePath, logPath })

    if (lease) {
      const fencedSpawn =
        'import os,subprocess,sys\n' +
        buildLeaseFenceOpen(lease) +
        'try:\n' +
        ` cmd=${shq(spawnCmd)}\n` +
        ' out=subprocess.check_output(cmd,shell=True,text=True,stderr=subprocess.STDOUT)\n' +
        ' sys.stdout.write(out if out.endswith("\\n") else out+"\\n")\n' +
        'finally:\n' +
        ' os.close(lease_fd)\n'

      out = await ssh.exec(`python3 -c ${shq(fencedSpawn)}`)
    } else {
      out = await ssh.exec(spawnCmd)
    }
  } catch (error) {
    await cleanupToken()
    throw error
  }

  const pid = parseInt(
    String(out || '')
      .trim()
      .split('\n')
      .pop(),
    10
  )

  if (!Number.isInteger(pid) || pid <= 0) {
    await cleanupToken()

    const err: any = new Error('Failed to launch the remote dashboard (no pid returned).')
    err.kind = 'spawn-failed'
    throw err
  }

  // Capture spawn-observed identity immediately after spawn. Launcher path is
  // metadata only; ownership uses this observed identity + nonce.
  let pidStartTime = null
  let processFingerprint = ''

  try {
    const observed = await observeProcessIdentity(ssh, pid, spawnNonce)

    if (observed) {
      pidStartTime = observed.startTime
      processFingerprint = observed.fingerprint || ''
    }
  } catch {
    // Observation failure is non-fatal at spawn; later classification may still
    // prove ownership via live serve --isolated --ssh-owner-nonce shape.
  }

  return { pid, spawnNonce, logPath, tokenFilePath, pidStartTime, processFingerprint }
}

// Best-effort forward teardown when a reuse attempt fails mid-flight, so we
// don't leak a forward before respawning. `deps.cancelForward` is optional.
async function cancelForwardSafe(deps, localPort, remotePort) {
  if (typeof deps.cancelForward !== 'function') {
    return
  }

  try {
    await deps.cancelForward(localPort, remotePort)
  } catch {
    // best effort
  }
}

function assertNotAborted(signal) {
  if (signal?.aborted) {
    const error: any = new Error('SSH bootstrap was cancelled.')
    error.kind = 'superseded'
    throw error
  }
}

function isForwardBindCollision(error) {
  return /address already in use|cannot listen to port|bind.*failed/i.test(String(error?.message || error || ''))
}

async function openForward(deps, remotePort, attempts = 3) {
  let lastError

  for (let attempt = 0; attempt < attempts; attempt++) {
    const localPort = await deps.pickLocalPort()

    try {
      await deps.forward(localPort, remotePort)

      return localPort
    } catch (error) {
      lastError = error

      if (!isForwardBindCollision(error) || attempt === attempts - 1) {
        throw error
      }
    }
  }

  throw lastError
}

/**
 * Cross-process remote mutex for the full read→classify→cleanup/reuse/spawn→write
 * transaction. Held through the final ready lock write.
 *
 * Owner-epoch protocol:
 *   - lease payload is `${holder}:${epoch}` (unguessable holder + unique epoch)
 *   - stale takeover reclaims in-place under flock (no stat-then-unlink race)
 *   - heartbeat refreshes mtime only while flock-holding the matching token
 *   - every spawn/write/remove mutation validates the token inside the same
 *     remote helper that performs the filesystem/process mutation
 */
async function withOwnershipMutex(ssh, ownershipId, fn, options: any = {}) {
  const directory = ownershipDirectory(ownershipId)
  const mutexPath = ownershipMutexPath(ownershipId)
  const holder = `${process.pid}-${crypto.randomBytes(4).toString('hex')}`
  const epoch = `${Date.now().toString(16)}-${crypto.randomBytes(4).toString('hex')}`
  const token = `${holder}:${epoch}`
  const heartbeatMs = options.heartbeatMs ?? OWNERSHIP_MUTEX_HEARTBEAT_MS

  const script =
    'import os,sys,time\n' +
    'try:\n' +
    ' import fcntl\n' +
    'except ImportError:\n' +
    ' fcntl=None\n' +
    `d=os.path.expanduser(${shq(directory)})\n` +
    `p=os.path.expanduser(${shq(mutexPath)})\n` +
    `token=${shq(token)}\n` +
    `stale_ms=${OWNERSHIP_MUTEX_STALE_MS}\n` +
    'os.makedirs(d,mode=0o700,exist_ok=True)\n' +
    'deadline=time.time()+30\n' +
    'while time.time()<deadline:\n' +
    ' try:\n' +
    '  fd=os.open(p,os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)\n' +
    '  try:\n' +
    '   os.write(fd,token.encode()); os.fsync(fd)\n' +
    '  finally:\n' +
    '   os.close(fd)\n' +
    '  print("ACQUIRED"); sys.exit(0)\n' +
    ' except FileExistsError:\n' +
    '  try:\n' +
    '   fd=os.open(p,os.O_RDWR)\n' +
    '  except FileNotFoundError:\n' +
    '   continue\n' +
    '  except OSError:\n' +
    '   time.sleep(0.05); continue\n' +
    '  try:\n' +
    '   if fcntl is not None:\n' +
    '    fcntl.flock(fd,fcntl.LOCK_EX)\n' +
    '   st=os.fstat(fd)\n' +
    '   age=(time.time()-st.st_mtime)*1000\n' +
    '   os.lseek(fd,0,os.SEEK_SET)\n' +
    '   previous=os.read(fd,4096).decode().strip()\n' +
    '   # Owner-epoch reclaim: rewrite the same inode under flock. Never\n' +
    '   # pathname-unlink a lease we have not proven stale while holding it.\n' +
    '   if previous=="RELEASED" or age>stale_ms:\n' +
    '    os.lseek(fd,0,os.SEEK_SET); os.ftruncate(fd,0)\n' +
    '    os.write(fd,token.encode()); os.fsync(fd)\n' +
    '    try:os.utime(p,None)\n' +
    '    except OSError:pass\n' +
    '    print("ACQUIRED"); sys.exit(0)\n' +
    '  finally:\n' +
    '   os.close(fd)\n' +
    '  time.sleep(0.05)\n' +
    'print("TIMEOUT"); sys.exit(2)'

  const acquireOut = String(await ssh.exec(`python3 -c ${shq(script)}`)).trim()

  if (!(acquireOut === 'ACQUIRED' || acquireOut.endsWith('\nACQUIRED') || acquireOut.endsWith('ACQUIRED'))) {
    const error: any = new Error('Could not acquire the SSH backend ownership mutex.')
    error.kind = 'transient-transport-error'
    throw error
  }

  let lostError: any = null
  let heartbeatChain = Promise.resolve()

  const markLost = error => {
    lostError = error

    return error
  }

  const runHeld = async bodyPython => {
    if (lostError) {
      throw lostError
    }

    const scriptBody =
      'import os,sys\n' +
      buildLeaseFenceOpen({ path: mutexPath, token }) +
      'try:\n' +
      indentPython(bodyPython, 1) +
      '\nfinally:\n' +
      ' os.close(lease_fd)\n'

    let out

    try {
      out = String(await ssh.exec(`python3 -c ${shq(scriptBody)}`))
    } catch (cause) {
      const error: any = new Error('Could not run a lease-fenced SSH lifecycle mutation.')
      error.kind = 'transient-transport-error'
      error.cause = cause
      throw markLost(error)
    }

    const lines = out.trim().split('\n').map(line => line.trim()).filter(Boolean)

    if (lines[0] === 'LOST') {
      throw markLost(
        ownershipConflict(
          'SSH backend ownership mutex was lost; refusing further lifecycle mutations.'
        )
      )
    }

    return out
  }

  const assertHeld = async () => {
    if (lostError) {
      throw lostError
    }

    const verifyScript =
      'import os,sys\n' +
      'try:\n' +
      ' import fcntl\n' +
      'except ImportError:\n' +
      ' fcntl=None\n' +
      `p=os.path.expanduser(${shq(mutexPath)})\n` +
      `token=${shq(token)}\n` +
      'OWNER_EPOCH_HEARTBEAT=1\n' +
      'try:\n' +
      ' fd=os.open(p,os.O_RDWR)\n' +
      'except OSError:\n' +
      ' print("LOST"); sys.exit(0)\n' +
      'try:\n' +
      ' if fcntl is not None:\n' +
      '  fcntl.flock(fd,fcntl.LOCK_EX)\n' +
      ' data=os.read(fd,4096).decode().strip()\n' +
      ' if data!=token:\n' +
      '  print("LOST"); sys.exit(0)\n' +
      ' try:os.utime(p,None)\n' +
      ' except OSError:pass\n' +
      ' print("HELD")\n' +
      'finally:\n' +
      ' os.close(fd)'

    let out

    try {
      out = String(await ssh.exec(`python3 -c ${shq(verifyScript)}`)).trim()
    } catch (cause) {
      const error: any = new Error('Could not heartbeat the SSH backend ownership mutex.')
      error.kind = 'transient-transport-error'
      error.cause = cause
      throw markLost(error)
    }

    if (!out.endsWith('HELD')) {
      throw markLost(
        ownershipConflict(
          'SSH backend ownership mutex was lost; refusing further lifecycle mutations.'
        )
      )
    }
  }

  const heartbeat = () => {
    heartbeatChain = heartbeatChain
      .then(() => assertHeld())
      .catch(error => {
        lostError = error
      })
  }

  const heartbeatTimer = setInterval(heartbeat, heartbeatMs)
  heartbeatTimer.unref?.()

  const lease = {
    holder,
    epoch,
    token,
    path: mutexPath,
    assertHeld,
    runHeld
  }

  try {
    const result = await fn(lease)
    clearInterval(heartbeatTimer)
    await heartbeatChain

    if (lostError) {
      throw lostError
    }

    return result
  } finally {
    clearInterval(heartbeatTimer)
    await heartbeatChain

    try {
      await ssh.exec(
        `python3 -c ${shq(
          'import os\n' +
            'try:\n' +
            ' import fcntl\n' +
            'except ImportError:\n' +
            ' fcntl=None\n' +
            `p=os.path.expanduser(${shq(mutexPath)})\n` +
            `token=${shq(token)}\n` +
            'OWNER_EPOCH_RELEASE=1\n' +
            'try:\n' +
            ' fd=os.open(p,os.O_RDWR)\n' +
            'except OSError:\n' +
            ' pass\n' +
            'else:\n' +
            ' try:\n' +
            '  if fcntl is not None:\n' +
            '   fcntl.flock(fd,fcntl.LOCK_EX)\n' +
            '  data=os.read(fd,4096).decode().strip()\n' +
            '  if data==token:\n' +
            '   # Publish a reclaimable epoch on the same inode. Unlinking after\n' +
            '   # unlock lets a queued waiter rewrite the old inode while a third\n' +
            '   # owner creates the pathname, producing two concurrent owners.\n' +
            '   os.lseek(fd,0,os.SEEK_SET); os.ftruncate(fd,0)\n' +
            '   os.write(fd,b"RELEASED"); os.fsync(fd)\n' +
            '   try:os.utime(p,(0,0))\n' +
            '   except OSError:pass\n' +
            '  os.close(fd)\n' +
            ' except Exception:\n' +
            '  try:os.close(fd)\n' +
            '  except OSError:pass'
        )}`
      )
    } catch {
      void 0
    }
  }
}

/**
 * Establish (or reuse) a remote dashboard and a tunnel to it. `deps` injects the
 * opened SshConnection, forward/pickLocalPort/waitForHermes, a token-gated
 * probeReuseProof, and adoptServedToken. Returns the connection descriptor
 * { baseUrl, token, tokenFingerprint, remotePort, localPort, pid, reused, platform }.
 */
async function adoptOwnedServedToken(adoptServedToken, baseUrl, expectedToken, ssh, pid, label) {
  const token = await adoptServedToken(baseUrl, expectedToken, {
    childAlive: () => true,
    label
  })

  if (!(await remotePidAlive(ssh, pid))) {
    const error: any = new Error(`${label} exited while its served token was being resolved.`)
    error.kind = token === expectedToken ? 'spawn-failed' : 'foreign-backend'
    throw error
  }

  return token
}

async function connect(deps) {
  const {
    ssh,
    profile = '',
    remoteHermesPath = '',
    ownershipId,
    forward,
    pickLocalPort,
    waitForHermes,
    probeReuseProof,
    adoptServedToken,
    rememberLog = () => {},
    readyTimeoutMs = DEFAULT_READY_TIMEOUT_MS,
    signal
  } = deps

  const log = msg => rememberLog(`[ssh-lifecycle] ${msg}`)

  assertNotAborted(signal)
  const platform = await probeRemotePlatform(ssh)
  log(`remote platform ${platform.os}/${platform.arch}`)
  const hermesPath = await locateHermes(ssh, remoteHermesPath)
  log(`located hermes at ${hermesPath}`)
  const hermesVersion = await probeHermesVersion(ssh, hermesPath)

  if (hermesVersion) {
    log(`remote hermes version: ${hermesVersion}`)
  }

  const reuseToken = deps.reuseToken || ''
  const hermesHome = await probeRemoteHermesHome(ssh)

  return withOwnershipMutex(ssh, ownershipId, async lease => {
    assertNotAborted(signal)
    await lease.assertHeld()
    const ownership = await readOwnershipRecord(ssh, ownershipId)
    const lock = ownership.record

    if (ownership.state === 'invalid') {
      const candidatePid = ownership.parsed?.pid

      if (Number.isInteger(candidatePid) && candidatePid > 0 && candidatePid <= 4194304) {
        if (await remotePidAlive(ssh, candidatePid)) {
          throw ownershipConflict(
            'An existing SSH backend ownership record is incompatible or malformed while its PID is alive.'
          )
        }

        await removeLockfile(ssh, ownershipId, lease)
      } else {
        throw ownershipConflict(
          'An existing SSH backend ownership record is malformed and cannot be proven stale.'
        )
      }
    }

    if (lock) {
      const pidAlive = await remotePidAlive(ssh, lock.pid)
      let classification = 'foreign'

      if (pidAlive) {
        classification = await classifyProcessOwnership(ssh, { ...lock, ownershipId })
      }

      const owned = pidAlive && classification === 'owned'

      if (pidAlive && !owned) {
        // Alive indeterminate/foreign: fail closed. Never delete lock/log or spawn.
        throw ownershipConflict(
          'An existing SSH backend lock is alive but not provably owned by this Desktop connection.'
        )
      }

      const reusable =
        owned &&
        lock.port > 0 &&
        lock.profile === profile &&
        Boolean(reuseToken) &&
        lock.tokenFingerprint === fingerprintToken(reuseToken) &&
        // The configured launcher is explicit runtime intent. It remains a
        // metadata comparison (not a live argv invariant), so exec wrappers
        // are still supported while a settings change forces replacement.
        (lock.launcherPath || lock.hermesPath) === hermesPath &&
        lock.hermesHome === hermesHome

      if (reusable) {
        assertNotAborted(signal)
        const localPort = await openForward(deps, lock.port)

        try {
          const baseUrl = `http://127.0.0.1:${localPort}`
          let reuseClassification

          try {
            reuseClassification = await probeReuseProof(baseUrl, reuseToken, lock.spawnNonce)
          } catch (cause) {
            const error: any = new Error('Could not verify the existing SSH backend.')
            error.kind = 'transient-transport-error'
            error.cause = cause
            throw error
          }

          if (reuseClassification === 'authenticated-stale') {
            assertNotAborted(signal)
            await cancelForwardSafe(deps, localPort, lock.port)
            await cleanupStale(ssh, ownershipId, lock, true, lease)
          } else if (reuseClassification === 'authenticated-ok') {
            const token = await adoptOwnedServedToken(
              adoptServedToken,
              baseUrl,
              reuseToken,
              ssh,
              lock.pid,
              'reused remote dashboard'
            )

            assertNotAborted(signal)
            log(`reusing remote dashboard pid=${lock.pid} port=${lock.port}`)

            return {
              baseUrl,
              token,
              tokenFingerprint: fingerprintToken(token),
              remotePort: lock.port,
              localPort,
              pid: lock.pid,
              reused: true,
              platform,
              hermesPath,
              hermesVersion,
              ownershipId,
              spawnNonce: lock.spawnNonce,
              logPath: lock.logPath
            }
          } else {
            const error: any = new Error('SSH reuse proof returned an invalid classification.')
            error.kind = 'transient-transport-error'
            throw error
          }
        } catch (error) {
          await cancelForwardSafe(deps, localPort, lock.port)
          throw error
        }
      } else if (owned) {
        // Owned but not reusable (profile/token/port mismatch): terminate exact
        // identity, wait until gone, then spawn. Never overlap two owners.
        assertNotAborted(signal)
        await cleanupStale(ssh, ownershipId, lock, pidAlive, lease)
      } else {
        // Dead pid: drop lock evidence and continue to spawn.
        assertNotAborted(signal)
        await cleanupStale(ssh, ownershipId, lock, false, lease)
      }
    }

    assertNotAborted(signal)
    const spawnToken = mintToken()

    const { pid, spawnNonce, logPath, tokenFilePath, pidStartTime, processFingerprint } =
      await spawnRemoteDashboard(ssh, {
        hermesPath,
        profile,
        token: spawnToken,
        ownershipId,
        lease
      })

    log(`spawned remote dashboard pid=${pid}`)

    const ownedSpawn = {
      ownershipId,
      spawnNonce,
      pid,
      port: 0,
      profile,
      hermesPath,
      launcherPath: hermesPath,
      hermesHome,
      logPath,
      tokenFingerprint: fingerprintToken(spawnToken),
      protocolVersion: PROTOCOL_VERSION,
      startedAt: new Date().toISOString(),
      pidStartTime,
      processFingerprint
    }

    let localPort = 0
    let remotePort = 0

    try {
      // Write the ownership record IMMEDIATELY (port=0): a supersede between
      // spawn and readiness whose cleanup cannot reach the box must not leave a
      // lockless orphan — the next connect reaps it by exact ownership via this
      // record. Inside the try: if this write itself fails, the catch still
      // kills the just-spawned process via the in-memory record.
      await writeLockfile(ssh, ownershipId, ownedSpawn, lease)
      remotePort = await scrapeReadyPort(ssh, logPath, {
        timeoutMs: readyTimeoutMs,
        isAlive: () => remotePidAlive(ssh, pid),
        signal
      })
      assertNotAborted(signal)
      log(`remote dashboard bound port ${remotePort}`)

      localPort = await openForward(deps, remotePort)
      assertNotAborted(signal)
      const baseUrl = `http://127.0.0.1:${localPort}`
      await waitForHermes(baseUrl, spawnToken)
      assertNotAborted(signal)

      const token = await adoptOwnedServedToken(adoptServedToken, baseUrl, spawnToken, ssh, pid, 'remote dashboard')

      assertNotAborted(signal)
      const tokenFingerprint = fingerprintToken(token)
      await writeLockfile(ssh, ownershipId, { ...ownedSpawn, port: remotePort, tokenFingerprint }, lease)
      assertNotAborted(signal)

      return {
        baseUrl,
        token,
        tokenFingerprint,
        remotePort,
        localPort,
        pid,
        reused: false,
        platform,
        hermesPath,
        hermesVersion,
        ownershipId,
        spawnNonce,
        logPath
      }
    } catch (error) {
      if (localPort && remotePort) {
        await cancelForwardSafe(deps, localPort, remotePort)
      }

      try {
        await lease.runHeld(
          'import os\n' +
            `p=os.path.expanduser(${shq(tokenFilePath)})\n` +
            'try:os.unlink(p)\n' +
            'except FileNotFoundError:pass\n' +
            'print("TOKEN_REMOVED")'
        )
      } catch {
        void 0
      }

      await cleanupStale(ssh, ownershipId, ownedSpawn, true, lease)
      throw error
    }
  })
}

export {
  adoptOwnedServedToken,
  buildSpawnCommand,
  classifyProcessOwnership,
  cleanupStale,
  connect,
  DEFAULT_READY_TIMEOUT_MS,
  expandRemotePath,
  fingerprintToken,
  isForwardBindCollision,
  locateHermes,
  LOCKFILE_SCHEMA_VERSION,
  lockfilePath,
  mintToken,
  observeProcessIdentity,
  openForward,
  ownershipConflict,
  ownershipDirectory,
  ownershipMutexPath,
  pidIsOurDashboard,
  probeHermesVersion,
  probeRemoteHermesHome,
  probeRemotePlatform,
  PROTOCOL_VERSION,
  readLockfile,
  READY_RE,
  REMOTE_LOCK_DIR,
  remotePidAlive,
  remoteSupportsSshOwnership,
  removeLockfile,
  scrapeReadyPort,
  shq,
  spawnLogPath,
  spawnRemoteDashboard,
  SUPPORTED_REMOTE_OS,
  terminateOwnedProcess,
  validateRemotePath,
  withOwnershipMutex,
  writeLockfile
}
