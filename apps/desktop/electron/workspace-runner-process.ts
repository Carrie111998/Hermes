import { type ChildProcessWithoutNullStreams, spawn } from 'node:child_process'
import { createHash, createHmac, randomBytes, randomUUID } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'
import readline from 'node:readline'

interface RunnerBackend {
  command: string
  env?: NodeJS.ProcessEnv
  root: string
}

interface RunnerProcessOptions {
  backend: RunnerBackend
  stateDirectory: string
}

interface PendingRequest {
  reject: (error: Error) => void
  resolve: (value: any) => void
  timer: NodeJS.Timeout
}

function canonicalJson(value: any): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(',')}]`
  }

  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`
  }

  return JSON.stringify(value)
}

export class WorkspaceRunnerProcess {
  private readonly backend: RunnerBackend
  private readonly deviceKey: Buffer
  private readonly pending = new Map<string, PendingRequest>()
  private process: ChildProcessWithoutNullStreams | null = null
  private readonly stateDirectory: string

  constructor(options: RunnerProcessOptions) {
    this.backend = options.backend
    this.stateDirectory = options.stateDirectory
    fs.mkdirSync(this.stateDirectory, { recursive: true, mode: 0o700 })
    const keyPath = path.join(this.stateDirectory, 'device.key')

    if (fs.existsSync(keyPath)) {
      this.deviceKey = fs.readFileSync(keyPath)
    } else {
      this.deviceKey = randomBytes(32)
      fs.writeFileSync(keyPath, this.deviceKey, { mode: 0o600 })
    }
  }

  private ensureStarted(): void {
    if (this.process && this.process.exitCode === null) {return}

    const child = spawn(
      this.backend.command,
      ['-m', 'hermes_cli.runner_process', '--state', path.join(this.stateDirectory, 'runner.db')],
      {
        cwd: this.backend.root,
        env: {
          ...process.env,
          ...this.backend.env,
          HERMES_RUNNER_DEVICE_KEY: this.deviceKey.toString('base64')
        },
        stdio: ['pipe', 'pipe', 'pipe']
      }
    )

    this.process = child
    child.stderr.on('data', () => undefined)
    const lines = readline.createInterface({ input: child.stdout })
    lines.on('line', line => {
      let response: any

      try {
        response = JSON.parse(line)
      } catch {
        return
      }

      const requestId = String(response?.request_id || '')
      const pending = this.pending.get(requestId)

      if (!pending) {return}

      clearTimeout(pending.timer)
      this.pending.delete(requestId)

      if (response.ok) {pending.resolve(response.result)}
      else {pending.reject(new Error(String(response.error || 'Workspace runner request failed.')))}
    })
    child.once('exit', (code, signal) => {
      this.process = null
      const error = new Error(`Workspace runner exited (${code ?? signal ?? 'unknown'}).`)

      for (const pending of this.pending.values()) {
        clearTimeout(pending.timer)
        pending.reject(error)
      }

      this.pending.clear()
    })
  }

  async call(method: string, params: Record<string, any>, timeoutMs = 30_000): Promise<any> {
    this.ensureStarted()
    const child = this.process

    if (!child) {throw new Error('Workspace runner is unavailable.')}

    const requestId = randomUUID()

    const response = new Promise<any>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId)
        reject(new Error(`Workspace runner request timed out: ${method}`))
      }, timeoutMs)

      this.pending.set(requestId, { reject, resolve, timer })
    })

    child.stdin.write(`${JSON.stringify({ method, params, request_id: requestId })}\n`)

    return response
  }

  private signCommand(command: Record<string, any>) {
    return {
      payload: command,
      protocol_version: 1,
      signature: createHmac('sha256', this.deviceKey).update(canonicalJson(command)).digest('hex')
    }
  }

  async worktreeAdd(
    repoPath: string,
    options: { base?: string; branch?: string; existingBranch?: string; name?: string }
  ) {
    const projectId = `local-${createHash('sha256').update(path.resolve(repoPath)).digest('hex').slice(0, 24)}`

    const binding = await this.call('binding.register', {
      label: path.basename(repoPath),
      path: repoPath,
      project_id: projectId
    })

    const lease = await this.call('lease.acquire', {
      binding_id: binding.binding_id,
      owner: `desktop-${randomUUID()}`,
      ttl_seconds: 120
    })

    const runId = randomUUID()
    const attemptId = randomUUID()

    try {
      const params = Object.fromEntries(
        Object.entries(options).filter(([, value]) => value !== undefined)
      )

      const command = {
        attempt_id: attemptId,
        binding_id: binding.binding_id,
        command_id: randomUUID(),
        fencing_token: lease.fencing_token,
        lease_id: lease.lease_id,
        method: 'git.worktree.add',
        params,
        protocol_version: 1,
        run_id: runId
      }

      const executed = await this.call(
        'command.execute',
        { envelope: this.signCommand(command) },
        180_000
      )

      const created = executed.result

      const [local, parentLocal] = await Promise.all([
        this.call('binding.resolve-local', {
          binding_id: created.binding.binding_id
        }),
        this.call('binding.resolve-local', {
          binding_id: binding.binding_id
        })
      ])

      return { branch: created.branch, path: local.path, repoRoot: parentLocal.path }
    } finally {
      await this.call('lease.release', {
        binding_id: binding.binding_id,
        fencing_token: lease.fencing_token,
        lease_id: lease.lease_id
      }).catch(() => undefined)
    }
  }

  stop(): void {
    const child = this.process
    this.process = null

    if (child && child.exitCode === null) {
      child.kill('SIGTERM')
    }
  }
}
