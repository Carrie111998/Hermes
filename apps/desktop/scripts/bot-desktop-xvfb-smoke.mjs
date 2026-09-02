import { execFile, spawn } from 'node:child_process'

const args = new Map(
  process.argv.slice(2).flatMap(value => {
    const [name, ...rest] = value.split('=')
    return name?.startsWith('--') && rest.length ? [[name.slice(2), rest.join('=')]] : []
  })
)
const provider = args.get('provider') || 'wsl'
const distro = args.get('distro') || 'Ubuntu'
const image = args.get('image') || 'hermes-bot-desktop-xvfb:local'
const displayStart = Number(args.get('display-start') || '101')
const timeoutMs = Number(args.get('timeout-ms') || '10000')

if (!['wsl', 'docker'].includes(provider)) {
  throw new Error('--provider must be wsl or docker')
}

if (!Number.isInteger(displayStart) || displayStart < 1 || displayStart > 9999) {
  throw new Error('--display-start must be an integer between 1 and 9999')
}

if (!Number.isInteger(timeoutMs) || timeoutMs < 250 || timeoutMs > 30_000) {
  throw new Error('--timeout-ms must be an integer between 250 and 30000')
}

function quote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`
}

function launcher(display, marker = '') {
  const ready = marker
    ? `printf '%s\\n' "$pid" > ${quote(marker)}`
    : `printf '__HERMES_BOT_DESKTOP_SMOKE_READY__=%s\\n' "$pid"`
  const probe = `DISPLAY=${quote(display)} xdpyinfo >/dev/null 2>&1`

  return [
    'set -eu',
    `Xvfb ${quote(display)} -screen 0 1280x800x24 -nolisten tcp >/tmp/hermes-bot-desktop-smoke.log 2>&1 & pid=$!`,
    'ready=0',
    'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do :',
    `  if ${probe}; then ${ready}; ready=1; break; fi`,
    '  if ! kill -0 "$pid" 2>/dev/null; then wait "$pid"; exit 1; fi',
    '  sleep 0.05',
    'done',
    'test "$ready" -eq 1',
    'wait "$pid"'
  ].join('; ')
}

function command(file, commandArgs) {
  return new Promise((resolve, reject) => {
    let output = ''
    let errorOutput = ''
    let settled = false
    const child = execFile(file, commandArgs, { timeout: timeoutMs, windowsHide: true }, (error, stdout, stderr) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      output = String(stdout || '')
      errorOutput = String(stderr || '')
      if (error) {
        reject(new Error(`${file} failed: ${error.message}${errorOutput.trim() ? `: ${errorOutput.trim()}` : ''}`))
        return
      }
      resolve(output)
    })
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      child.kill('SIGTERM')
      reject(new Error(`${file} timed out after ${timeoutMs}ms`))
    }, timeoutMs)
  })
}

function wslSession(display) {
  const child = spawn('wsl.exe', ['-d', distro, '--exec', 'sh', '-lc', launcher(display)], {
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true
  })
  let output = ''
  let errorOutput = ''

  const ready = new Promise((resolve, reject) => {
    let settled = false
    const timer = setTimeout(() => {
      if (settled) return
      settled = true
      child.kill('SIGTERM')
      reject(new Error(`WSL Xvfb did not become ready on ${display}`))
    }, timeoutMs)
    child.stdout.on('data', chunk => {
      output += String(chunk)
      const match = output.match(/__HERMES_BOT_DESKTOP_SMOKE_READY__=(\d+)/)
      if (!match || settled) return
      settled = true
      clearTimeout(timer)
      resolve(Number(match[1]))
    })
    child.stderr.on('data', chunk => {
      errorOutput += String(chunk)
    })
    child.once('error', error => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      reject(error)
    })
    child.once('exit', (code, signal) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      reject(new Error(`WSL exited before ready (code=${String(code)}, signal=${String(signal)}): ${errorOutput.trim()}`))
    })
  })

  return { child, ready }
}

async function dockerSession(profile, display, runId) {
  const name = `hermes-bot-desktop-smoke-${runId}-${profile}`
  const marker = `/tmp/hermes-bot-desktop-smoke-${profile}.ready`
  await command('docker.exe', ['run', '--detach', '--name', name, image, 'sh', '-lc', launcher(display, marker)])
  await command('docker.exe', ['exec', name, 'sh', '-lc', `test -s ${quote(marker)} && DISPLAY=${quote(display)} xdpyinfo >/dev/null 2>&1`])
  return { name }
}

async function stopWsl(session) {
  try {
    await command('wsl.exe', ['-d', distro, '--exec', 'kill', '-TERM', String(await session.ready)])
  } catch {
    session.child.kill('SIGTERM')
  }
}

async function stopDocker(session) {
  try {
    await command('docker.exe', ['rm', '--force', session.name])
  } catch {
    // The original smoke failure is more useful than cleanup noise.
  }
}

const runId = `${process.pid}-${Date.now()}`
const sessions = []

try {
  const displays = [`:${displayStart}`, `:${displayStart + 1}`]
  const first = provider === 'wsl' ? wslSession(displays[0]) : { ready: dockerSession('alpha', displays[0], runId) }
  sessions.push(first)
  const second = provider === 'wsl' ? wslSession(displays[1]) : { ready: dockerSession('research', displays[1], runId) }
  sessions.push(second)
  await Promise.all(sessions.map(session => session.ready))
  console.log(JSON.stringify({ status: 'verified', provider, displays, distinct: true }, null, 2))
} catch (error) {
  console.error(JSON.stringify({ status: 'blocked', provider, reason: error instanceof Error ? error.message : String(error) }, null, 2))
  process.exitCode = 2
} finally {
  await Promise.all(
    sessions.map(session =>
      provider === 'wsl' ? stopWsl(session) : session.ready.then(value => stopDocker(value)).catch(() => undefined)
    )
  )
}
