import { spawn } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { app, BrowserWindow, ipcMain, nativeTheme } from 'electron'

export const SIGIL_APP_NAME = 'Sigil'
export const SIGIL_BUNDLE_ID = 'com.firecattechnology.sigil'
export const SIGIL_USER_DATA_DIRECTORY = 'Sigil'
export const SIGIL_BACKEND_STATUS_CHANNEL = 'sigil:get-backend-status'
export const SIGIL_EXPLAIN_PROPOSAL_CHANNEL = 'sigil:explain-proposal'

type BackendStatus = {
  bridge_version: string
  status: string
  mode: string
  environment: string
  simulation: boolean
  execution_authorized: boolean
  broker_submission_available: boolean
  supported_commands: string[]
}

type BackendResponse =
  | {
      ok: true
      result: BackendStatus
    }
  | {
      ok: false
      error: string
      message: string
    }

app.setName(SIGIL_APP_NAME)
nativeTheme.themeSource = 'dark'

const currentDirectory = path.dirname(fileURLToPath(import.meta.url))

function repositoryRoot(): string {
  return path.resolve(currentDirectory, '../../..')
}

function pythonExecutable(): string {
  return process.env.SIGIL_PYTHON || 'python'
}

type BridgeRequest = Readonly<{
  command: string
  payload?: Readonly<Record<string, unknown>>
}>

type BridgeResponse<T = unknown> =
  | {
      ok: true
      result: T
    }
  | {
      ok: false
      error: string
      message: string
    }

export function runBridgeRequest<T>(
  request: BridgeRequest
): Promise<BridgeResponse<T>> {
  return new Promise(resolve => {
    const root = repositoryRoot()
    const sourceRoot = path.join(root, 'apps/sigil/src')

    const child = spawn(
      pythonExecutable(),
      ['-m', 'sigil.desktop_bridge.runner'],
      {
        cwd: root,
        env: {
          ...process.env,
          PYTHONPATH: sourceRoot
        },
        stdio: ['pipe', 'pipe', 'pipe']
      }
    )

    let stdout = ''
    let stderr = ''
    let settled = false

    const finish = (response: BridgeResponse<T>): void => {
      if (settled) {
        return
      }

      settled = true
      resolve(response)
    }

    const timeout = setTimeout(() => {
      child.kill()
      finish({
        ok: false,
        error: 'backend_timeout',
        message: 'The local Sigil backend did not respond in time.'
      })
    }, 5000)

    child.stdout.setEncoding('utf8')
    child.stderr.setEncoding('utf8')

    child.stdout.on('data', (chunk: string) => {
      stdout += chunk
    })

    child.stderr.on('data', (chunk: string) => {
      stderr += chunk
    })

    child.on('error', () => {
      clearTimeout(timeout)
      finish({
        ok: false,
        error: 'backend_unavailable',
        message: 'The local Sigil backend could not be started.'
      })
    })

    child.on('close', () => {
      clearTimeout(timeout)

      try {
        const parsed = JSON.parse(stdout) as BridgeResponse<T>

        if (parsed && typeof parsed === 'object' && 'ok' in parsed) {
          finish(parsed)

          return
        }
      } catch {
        // Fail closed below.
      }

      finish({
        ok: false,
        error: 'invalid_backend_response',
        message:
          stderr.trim() ||
          'The local Sigil backend returned an invalid response.'
      })
    })

    child.stdin.end(`${JSON.stringify(request)}\n`)
  })
}

export function readBackendStatus(): Promise<BackendResponse> {
  return runBridgeRequest<BackendStatus>({ command: 'health' })
}

export function registerSigilIpc(): void {
  ipcMain.removeHandler(SIGIL_BACKEND_STATUS_CHANNEL)
  ipcMain.removeHandler(SIGIL_EXPLAIN_PROPOSAL_CHANNEL)

  ipcMain.handle(SIGIL_BACKEND_STATUS_CHANNEL, () => readBackendStatus())

  ipcMain.handle(
    SIGIL_EXPLAIN_PROPOSAL_CHANNEL,
    (_event, payload: Readonly<Record<string, unknown>>) =>
      runBridgeRequest({
        command: 'explain_proposal',
        payload
      })
  )
}

export function createSigilWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 900,
    minHeight: 720,
    title: SIGIL_APP_NAME,
    backgroundColor: '#0d0d0e',
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(currentDirectory, 'electron-preload.cjs')
    }
  })

  window.once('ready-to-show', () => window.show())
  const developmentServer = process.env.SIGIL_DEV_SERVER

  if (developmentServer) {
    void window.loadURL(developmentServer)
  } else {
    void window.loadFile(path.join(currentDirectory, 'index.html'))
  }

  return window
}

app.whenReady().then(() => {
  registerSigilIpc()
  createSigilWindow()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createSigilWindow()
    }
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})
