import crypto from 'node:crypto'

import { assertBootstrapNotSuperseded, redactSecrets, SSH_ERROR } from './ssh-connection'

const LOCKFILE_SCHEMA_VERSION = 2
const PROTOCOL_VERSION = 1
const READY_RE = /^HERMES_(?:BACKEND|DASHBOARD)_READY port=(\d+)/gm
const READY_POLL_INTERVAL_MS = 750

function psLiteral(value) {
  return `'${String(value).replace(/'/g, "''")}'`
}

function encodedPowerShell(script) {
  return Buffer.from(script, 'utf16le').toString('base64')
}

function powerShellCommand(script) {
  return `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ${encodedPowerShell(script)}`
}

async function probeWindowsRemote(ssh, explicitHermesPath = '') {
  const explicit = psLiteral(explicitHermesPath)

  const script = [
    '$ErrorActionPreference="Stop"',
    `$explicit=${explicit}`,
    '$hermesHome=$env:HERMES_HOME',
    'if(-not $hermesHome){$hermesHome=Join-Path $env:LOCALAPPDATA "hermes"}',
    '$candidates=@()',
    'if($explicit){$candidates+=$explicit}',
    '$cmd=Get-Command hermes.exe -ErrorAction SilentlyContinue',
    'if($cmd){$candidates+=$cmd.Source}',
    '$candidates+=(Join-Path $hermesHome "hermes-agent\\venv\\Scripts\\hermes.exe")',
    '$candidates+=(Join-Path $HOME "hermes-agent\\.venv\\Scripts\\hermes.exe")',
    '$hermes=$candidates|Where-Object{Test-Path -LiteralPath $_ -PathType Leaf}|Select-Object -First 1',
    'if(-not $hermes){throw "Hermes is not installed on the remote Windows host."}',
    'if($explicit -and $hermes -ne $explicit){throw "The configured Hermes path is not an executable file."}',
    '$python=Join-Path (Split-Path $hermes) "python.exe"',
    'if(-not (Test-Path -LiteralPath $python -PathType Leaf)){throw "The remote Hermes Python runtime was not found."}',
    '[ordered]@{os="Windows";arch=$env:PROCESSOR_ARCHITECTURE;hermesHome=$hermesHome;hermesPath=$hermes;python=$python}|ConvertTo-Json -Compress'
  ].join(';')

  return JSON.parse((await ssh.exec(powerShellCommand(script))).trim())
}

function windowsUpdateMarkerProbeCommand(hermesHome) {
  const script = [
    '$ErrorActionPreference="Stop"',
    `$home=${psLiteral(hermesHome)}`,
    '$installRoot=$home',
    '$parent=Split-Path -Parent $home',
    'if((Split-Path -Leaf $parent) -ieq "profiles"){$installRoot=Split-Path -Parent $parent}',
    '$marker=Join-Path $installRoot ".hermes-update-in-progress"',
    '$result="UNCERTAIN"',
    'try{',
    'if(-not [IO.File]::Exists($marker)){$result="CLEAR"}',
    'else{',
    '$bytes=[IO.File]::ReadAllBytes($marker)',
    'if($bytes.Length -le 256){',
    '$utf8=[Text.UTF8Encoding]::new($false,$true)',
    '$text=$utf8.GetString($bytes)',
    "$match=[regex]::Match($text,'\\A([1-9][0-9]*)\\r?\\n([0-9]+)(?:\\r?\\n)?\\z')",
    '[uint32]$ownerPid=0',
    '[uint64]$lease=0',
    '$valid=$match.Success',
    'if($valid){$valid=[uint32]::TryParse($match.Groups[1].Value,[Globalization.NumberStyles]::None,[Globalization.CultureInfo]::InvariantCulture,[ref]$ownerPid)}',
    'if($valid){$valid=[uint64]::TryParse($match.Groups[2].Value,[Globalization.NumberStyles]::None,[Globalization.CultureInfo]::InvariantCulture,[ref]$lease)}',
    'if($valid -and $lease -le 9007199254740991){',
    'try{',
    '$process=[Diagnostics.Process]::GetProcessById([int]$ownerPid)',
    'try{if($process.HasExited){$result="CLEAR"}else{$result="LIVE:"+[string]$ownerPid}}finally{$process.Dispose()}',
    '}catch [ArgumentException]{$result="CLEAR"} catch{$result="UNCERTAIN"}',
    '}',
    '}',
    '}',
    '}catch{$result="UNCERTAIN"}',
    'Write-Output $result'
  ].join(';')

  return powerShellCommand(script)
}

/**
 * Fail-closed install marker gate for a fresh/relaunched Desktop process.
 * This uses only PowerShell/.NET and therefore never imports the remote
 * checkout while an updater may be replacing it.
 */
async function assertWindowsRemoteInstallUpdateClear(ssh, hermesHome) {
  let observation = ''

  try {
    observation =
      String(await ssh.exec(windowsUpdateMarkerProbeCommand(hermesHome)))
        .replace(/^\uFEFF/, '')
        .trim()
        .split(/\r?\n/)
        .pop() || ''
  } catch (cause) {
    const error: any = new Error('Could not prove that the remote Hermes install is clear for SSH startup.')
    error.kind = 'update-in-progress'
    error.cause = cause
    throw error
  }

  if (observation === 'CLEAR') {
    return
  }

  const live = /^LIVE:([1-9][0-9]*)$/.exec(observation)
  const error: any = new Error(
    live
      ? `Remote Hermes update process ${live[1]} is still running; SSH startup is paused.`
      : 'The remote Hermes update marker is unreadable or malformed; refusing SSH startup.'
  )
  error.kind = 'update-in-progress'
  throw error
}

const TRANSPORT_KINDS = new Set([
  SSH_ERROR.AUTH_FAILED,
  SSH_ERROR.HOST_KEY_CHANGED,
  SSH_ERROR.TIMEOUT,
  SSH_ERROR.UNREACHABLE
])

async function detectRemotePlatform(ssh, explicitHermesPath = '') {
  try {
    const output = (await ssh.exec('uname -s; uname -m')).trim().split('\n')

    if (output[0] === 'Linux' || output[0] === 'Darwin') {
      return { os: output[0], arch: output[1] || '' }
    }
  } catch (error: any) {
    // uname failing is the expected Windows fall-through; a TRANSPORT failure
    // (auth/host-key/timeout/unreachable) is not a platform verdict — surface it
    // as itself instead of letting the probe chain end in 'unsupported-platform'.
    if (TRANSPORT_KINDS.has(error?.kind)) {
      throw error
    }
  }

  try {
    return await probeWindowsRemote(ssh, explicitHermesPath)
  } catch (cause: any) {
    if (TRANSPORT_KINDS.has(cause?.kind)) {
      throw cause
    }

    // detail is remote-controlled output headed for the UI: redact + strip control chars.
    const detail = redactSecrets(String(cause?.message || cause || ''))
      // eslint-disable-next-line no-control-regex -- deliberately strip control chars from remote output
      .replace(/[\x00-\x1f\x7f]/g, ' ')
      .trim()

    const error: any = new Error(
      `The remote operating system is not supported by Desktop SSH.${detail ? ` (probe: ${detail.slice(0, 300)})` : ''}`
    )

    error.kind = 'unsupported-platform'
    error.cause = cause
    throw error
  }
}

function helperCommand(runtime, operation, args = []) {
  const argv = [runtime.python, '-m', 'hermes_cli.windows_ssh_runtime', operation, ...args]

  const script = [
    '$ErrorActionPreference="Stop"',
    `& ${argv.map(psLiteral).join(' ')}`,
    'if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}'
  ].join(';')

  return powerShellCommand(script)
}

async function helper(ssh, runtime, operation, args = [], stdinData?) {
  const output = await ssh.exec(helperCommand(runtime, operation, args), stdinData == null ? {} : { stdinData })

  const lines = String(output || '')
    .replace(/^\uFEFF/, '')
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)

  const parsed = JSON.parse(lines[lines.length - 1] || 'null')

  if (parsed?.error) {
    throw new Error(parsed.error)
  }

  return parsed
}

function atomicWindowsSpawnCommand(runtime) {
  const argv = [runtime.python, '-m', 'hermes_cli.windows_ssh_runtime', 'spawn']
  const script = [
    '$ErrorActionPreference="Stop"',
    `$home=${psLiteral(runtime.hermesHome)}`,
    '$installRoot=$home',
    '$parent=Split-Path -Parent $home',
    'if((Split-Path -Leaf $parent) -ieq "profiles"){$installRoot=Split-Path -Parent $parent}',
    '$marker=Join-Path $installRoot ".hermes-update-in-progress"',
    '$mutexPath=$marker+".mutex"',
    '$mutex=[IO.File]::Open($mutexPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite)',
    'try{',
    '  $mutex.Lock(0,1)',
    '  if([IO.File]::Exists($marker)){throw "remote update marker is present"}',
    `  & ${argv.map(psLiteral).join(' ')}`,
    '  if($LASTEXITCODE -ne 0){exit $LASTEXITCODE}',
    '  if([IO.File]::Exists($marker)){throw "remote update marker claimed during backend spawn"}',
    '}finally{try{$mutex.Unlock(0,1)}catch{};$mutex.Dispose()}'
  ].join(';')
  return powerShellCommand(script)
}

async function atomicWindowsSpawn(ssh, runtime, stdinData) {
  const output = await ssh.exec(atomicWindowsSpawnCommand(runtime), { stdinData })
  const lines = String(output || '')
    .replace(/^\uFEFF/, '')
    .trim()
    .split(/\r?\n/)
    .filter(Boolean)
  const parsed = JSON.parse(lines[lines.length - 1] || 'null')

  if (parsed?.error) {
    const error: any = new Error(parsed.error)
    error.kind = parsed.kind || 'remote-helper-error'
    throw error
  }

  return parsed
}

function fingerprintToken(token) {
  return crypto
    .createHash('sha256')
    .update(String(token || ''))
    .digest('hex')
    .slice(0, 32)
}

function validLock(lock, ownershipId) {
  // port 0 = spawn-in-progress record (written before readiness); a valid
  // ownership proof for cleanup, but never reusable.
  return Boolean(
    lock &&
    lock.schemaVersion === LOCKFILE_SCHEMA_VERSION &&
    lock.protocolVersion === PROTOCOL_VERSION &&
    lock.ownershipId === ownershipId &&
    /^[0-9a-f]{16}$/.test(lock.spawnNonce || '') &&
    Number.isInteger(lock.pid) &&
    lock.pid > 0 &&
    /^[0-9]{10,20}$/.test(lock.creationTimeNs || '') &&
    Number.isInteger(lock.port) &&
    lock.port >= 0 &&
    lock.port <= 65535 &&
    /^[0-9a-f]{32}$/.test(lock.tokenFingerprint || '') &&
    typeof lock.hermesPath === 'string' &&
    typeof lock.hermesHome === 'string'
  )
}

function reusableWindowsLock(lock, state, profile, reuseToken, runtime) {
  return Boolean(
    state.alive &&
    state.owned &&
    lock.port > 0 &&
    lock.profile === profile &&
    reuseToken &&
    lock.tokenFingerprint === fingerprintToken(reuseToken) &&
    lock.hermesPath === runtime.hermesPath &&
    lock.hermesHome === runtime.hermesHome
  )
}

async function processState(ssh, runtime, lock) {
  return helper(ssh, runtime, 'process-state', [
    String(lock.pid),
    String(lock.creationTimeNs),
    lock.hermesPath,
    lock.spawnNonce
  ])
}

async function cleanupOwned(ssh, runtime, ownershipId, lock) {
  const attempt = async fn => {
    try {
      await fn()
    } catch {
      void 0
    }
  }

  if (lock) {
    const state = await processState(ssh, runtime, lock)

    if (state.alive && state.owned) {
      // Deliberately not attempt()-wrapped: a thrown terminate must abort before
      // remove-lock, or a live backend is orphaned with no lock to reclaim it.
      await helper(ssh, runtime, 'terminate', [
        String(lock.pid),
        String(lock.creationTimeNs),
        lock.hermesPath,
        lock.spawnNonce
      ])
    }

    if (lock.spawnNonce) {
      await attempt(() => helper(ssh, runtime, 'remove-token', [ownershipId, lock.spawnNonce]))
      await attempt(() => helper(ssh, runtime, 'remove-log', [ownershipId, lock.spawnNonce]))
    }
  }

  await attempt(() => helper(ssh, runtime, 'remove-lock', [ownershipId]))
}

function windowsLockMatchesManagedUpdateScope(lock, expected) {
  return Boolean(
    lock &&
    expected &&
    lock.ownershipId === expected.ownershipId &&
    lock.pid === expected.pid &&
    lock.spawnNonce === expected.spawnNonce &&
    lock.creationTimeNs === expected.creationTimeNs &&
    lock.profile === expected.profile &&
    lock.hermesPath === expected.hermesPath &&
    lock.hermesHome === expected.hermesHome
  )
}

/**
 * Terminate a Windows serve only after the persisted ownership record and the
 * kernel creation-time proof still match the exact scope Desktop connected.
 * Leave the record in place for the post-update reconnect to reclaim; this
 * prevents a delayed cleanup from deleting a replacement owner's record.
 */
async function terminateOwnedWindowsDashboardForUpdate(ssh, runtime, expected) {
  let lock = await helper(ssh, runtime, 'read-lock', [expected?.ownershipId || ''])

  if (!validLock(lock, expected?.ownershipId) || !windowsLockMatchesManagedUpdateScope(lock, expected)) {
    const error: any = new Error('The remote Windows ownership record changed before the managed update.')
    error.kind = 'ownership-changed'
    throw error
  }

  let state = await processState(ssh, runtime, lock)

  if (state.indeterminate) {
    const error: any = new Error('Could not prove the remote Windows process identity for the managed update.')
    error.kind = 'transient-transport-error'
    throw error
  }
  if (!state.alive) {
    return { pid: lock.pid, terminated: false, alreadyStopped: true }
  }
  if (!state.owned) {
    const error: any = new Error('Refusing to terminate a remote Windows process whose ownership is unproven.')
    error.kind = 'foreign-backend'
    throw error
  }

  // Fence the proof against a record replacement before signalling.
  lock = await helper(ssh, runtime, 'read-lock', [expected.ownershipId])

  if (!validLock(lock, expected.ownershipId) || !windowsLockMatchesManagedUpdateScope(lock, expected)) {
    const error: any = new Error('The remote Windows ownership record changed during process verification.')
    error.kind = 'ownership-changed'
    throw error
  }
  state = await processState(ssh, runtime, lock)

  if (state.indeterminate || !state.alive || !state.owned) {
    const error: any = new Error('The remote Windows process identity changed during managed update drain.')
    error.kind = state.indeterminate ? 'transient-transport-error' : 'ownership-changed'
    throw error
  }

  await helper(ssh, runtime, 'terminate', [
    String(lock.pid),
    String(lock.creationTimeNs),
    lock.hermesPath,
    lock.spawnNonce
  ])

  return { pid: lock.pid, terminated: true, alreadyStopped: false }
}

async function waitReady(ssh, runtime, ownershipId, lock, timeoutMs, signal) {
  const deadline = Date.now() + timeoutMs

  while (Date.now() < deadline) {
    assertBootstrapNotSuperseded(signal)
    let state

    try {
      state = await processState(ssh, runtime, lock)
    } catch {
      await new Promise(resolve => setTimeout(resolve, READY_POLL_INTERVAL_MS))

      continue
    }

    if (!state.indeterminate && (!state.alive || !state.owned)) {
      let detail = ''

      try {
        detail = (await helper(ssh, runtime, 'read-log', [ownershipId, lock.spawnNonce]))?.content || ''
      } catch {
        void 0
      }

      const error: any = new Error(
        `Remote Windows backend exited before announcing its port. state=${JSON.stringify(state)} ${detail.slice(-2000)}`
      )

      error.kind = 'spawn-failed'
      throw error
    }

    let content = ''

    try {
      content = (await helper(ssh, runtime, 'read-log', [ownershipId, lock.spawnNonce]))?.content || ''
    } catch {
      void 0
    }

    let port

    for (const match of content.matchAll(READY_RE)) {
      port = Number(match[1])
    }

    if (port) {
      return port
    }

    await new Promise(resolve => setTimeout(resolve, READY_POLL_INTERVAL_MS))
  }

  const error: any = new Error(`Timed out waiting for the remote Windows backend (${timeoutMs}ms).`)
  error.kind = 'ready-timeout'
  throw error
}

async function connectWindowsRemote(deps) {
  const {
    ssh,
    ownershipId,
    profile = '',
    remoteHermesPath = '',
    reuseToken = '',
    signal,
    pickLocalPort,
    forward,
    cancelForward,
    waitForHermes,
    probeReuseProof,
    rememberLog = () => {},
    readyTimeoutMs = 45_000
  } = deps

  assertBootstrapNotSuperseded(signal)
  const runtime = await probeWindowsRemote(ssh, remoteHermesPath)
  await assertWindowsRemoteInstallUpdateClear(ssh, runtime.hermesHome)
  const inspection = await helper(ssh, runtime, 'inspect', [runtime.hermesPath])

  if (!inspection.supported) {
    const error: any = new Error('Update Hermes on the remote Windows host before connecting with Desktop SSH.')
    error.kind = 'update-required'
    throw error
  }

  runtime.hermesPath = inspection.path
  const hermesVersion = inspection.version || ''
  rememberLog(`[ssh-lifecycle] remote platform Windows/${runtime.arch}`)
  rememberLog(`[ssh-lifecycle] located hermes at ${runtime.hermesPath}`)

  await assertWindowsRemoteInstallUpdateClear(ssh, runtime.hermesHome)
  const lock = await helper(ssh, runtime, 'read-lock', [ownershipId])

  if (validLock(lock, ownershipId)) {
    const state = await processState(ssh, runtime, lock)

    if (state.indeterminate) {
      const error: any = new Error('Could not determine the state of the existing remote backend.')
      error.kind = 'transient-transport-error'
      throw error
    }

    const reusable = reusableWindowsLock(lock, state, profile, reuseToken, runtime)

    if (reusable) {
      await assertWindowsRemoteInstallUpdateClear(ssh, runtime.hermesHome)
      const localPort = await pickLocalPort()
      await forward(localPort, lock.port)

      try {
        const baseUrl = `http://127.0.0.1:${localPort}`
        const classification = await probeReuseProof(baseUrl, reuseToken, lock.spawnNonce)

        if (classification === 'authenticated-ok') {
          return {
            baseUrl,
            token: reuseToken,
            remotePort: lock.port,
            localPort,
            pid: lock.pid,
            reused: true,
            platform: { os: 'Windows', arch: runtime.arch },
            hermesPath: runtime.hermesPath,
            hermesVersion,
            ownershipId,
            spawnNonce: lock.spawnNonce,
            creationTimeNs: lock.creationTimeNs,
            hermesHome: runtime.hermesHome,
            pythonPath: runtime.python
          }
        }

        if (classification !== 'authenticated-stale') {
          throw new Error('Invalid SSH reuse classification.')
        }

        await cancelForward(localPort, lock.port)
        await assertWindowsRemoteInstallUpdateClear(ssh, runtime.hermesHome)
        await cleanupOwned(ssh, runtime, ownershipId, lock)
      } catch (error) {
        await cancelForward(localPort, lock.port)
        throw error
      }
    } else {
      await assertWindowsRemoteInstallUpdateClear(ssh, runtime.hermesHome)
      await cleanupOwned(ssh, runtime, ownershipId, lock)
    }
  } else if (lock) {
    await assertWindowsRemoteInstallUpdateClear(ssh, runtime.hermesHome)
    await helper(ssh, runtime, 'remove-lock', [ownershipId])
  }

  assertBootstrapNotSuperseded(signal)
  await assertWindowsRemoteInstallUpdateClear(ssh, runtime.hermesHome)
  const token = crypto.randomBytes(32).toString('hex')
  const spawnNonce = crypto.randomBytes(8).toString('hex')
  await helper(ssh, runtime, 'upload-token', [ownershipId, spawnNonce], token)
  let spawned

  try {
    await assertWindowsRemoteInstallUpdateClear(ssh, runtime.hermesHome)
    spawned = await atomicWindowsSpawn(
      ssh,
      runtime,
      JSON.stringify({ ownershipId, spawnNonce, profile, hermesPath: runtime.hermesPath })
    )
  } catch (error) {
    await helper(ssh, runtime, 'remove-token', [ownershipId, spawnNonce])
    throw error
  }

  const owned = {
    schemaVersion: LOCKFILE_SCHEMA_VERSION,
    protocolVersion: PROTOCOL_VERSION,
    ownershipId,
    spawnNonce,
    pid: spawned.pid,
    creationTimeNs: spawned.creationTimeNs,
    port: 0,
    profile,
    hermesPath: runtime.hermesPath,
    hermesHome: runtime.hermesHome,
    tokenFingerprint: fingerprintToken(token),
    startedAt: new Date().toISOString()
  }

  let localPort = 0
  let remotePort = 0

  try {
    // Write the ownership record IMMEDIATELY (port=0): if this attempt is
    // superseded before readiness and cleanup cannot reach the box, the next
    // connect still finds the lock and reaps the process by exact ownership.
    // Inside the try: if this write itself fails, the catch still kills the
    // just-spawned process via the in-memory record.
    await helper(ssh, runtime, 'write-lock', [ownershipId], JSON.stringify(owned))
    remotePort = await waitReady(ssh, runtime, ownershipId, owned, readyTimeoutMs, signal)
    localPort = await pickLocalPort()
    await forward(localPort, remotePort)
    const baseUrl = `http://127.0.0.1:${localPort}`
    await waitForHermes(baseUrl, token)
    assertBootstrapNotSuperseded(signal)
    await helper(ssh, runtime, 'write-lock', [ownershipId], JSON.stringify({ ...owned, port: remotePort }))

    return {
      baseUrl,
      token,
      remotePort,
      localPort,
      pid: spawned.pid,
      reused: false,
      platform: { os: 'Windows', arch: runtime.arch },
      hermesPath: runtime.hermesPath,
      hermesVersion,
      ownershipId,
      spawnNonce,
      creationTimeNs: spawned.creationTimeNs,
      hermesHome: runtime.hermesHome,
      pythonPath: runtime.python
    }
  } catch (error) {
    if (localPort && remotePort) {
      await cancelForward(localPort, remotePort)
    }

    await cleanupOwned(ssh, runtime, ownershipId, owned)
    throw error
  }
}

function buildWindowsInteractiveCommand(remoteCwd = '') {
  const cwd = String(remoteCwd || '').trim()
  const script = ['$ErrorActionPreference="Stop"']

  if (cwd) {
    script.push(
      `if(Test-Path -LiteralPath ${psLiteral(cwd)} -PathType Container){Set-Location -LiteralPath ${psLiteral(cwd)}}`
    )
  }

  script.push('$host.UI.RawUI.WindowTitle="Hermes SSH"', 'powershell.exe -NoLogo')

  return powerShellCommand(script.join(';'))
}

export {
  assertWindowsRemoteInstallUpdateClear,
  atomicWindowsSpawnCommand,
  buildWindowsInteractiveCommand,
  connectWindowsRemote,
  detectRemotePlatform,
  encodedPowerShell,
  helper,
  helperCommand,
  powerShellCommand,
  probeWindowsRemote,
  psLiteral,
  reusableWindowsLock,
  terminateOwnedWindowsDashboardForUpdate,
  validLock
}
