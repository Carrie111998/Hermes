import type { App } from 'electron'

export const E2E_RENDERER_STARTUP_ERRORS_PROPERTY = '__hermesE2EStartupRendererErrors'
export const E2E_RENDERER_STARTUP_PENDING_PROPERTY = '__hermesE2EStartupRendererPending'
const E2E_STARTUP_RENDERER_ERROR_SENTINEL = 'HERMES_E2E_STARTUP_RENDERER_ERROR_SENTINEL'

interface ObservedApp extends App {
  [E2E_RENDERER_STARTUP_ERRORS_PROPERTY]?: string[]
  [E2E_RENDERER_STARTUP_PENDING_PROPERTY]?: Promise<void>[]
}

interface ConsoleMessageDetails {
  level: number
  message: string
}

/**
 * Test-only renderer diagnostics installed before Desktop creates any window.
 * The normal Playwright Page observer can attach after the initial renderer has
 * already emitted an error; this bounded buffer closes that startup interval.
 */
export function installE2ERendererStartupObserver(app: App, env: NodeJS.ProcessEnv = process.env): void {
  if (env.HERMES_E2E_OBSERVE_RENDERER_STARTUP !== '1') {
    return
  }

  const observedApp = app as ObservedApp

  if (observedApp[E2E_RENDERER_STARTUP_ERRORS_PROPERTY]) {
    return
  }

  const errors: string[] = []
  const pending: Promise<void>[] = []
  const record = (kind: string, detail: unknown) => errors.push(`${kind}: ${String(detail)}`)

  observedApp[E2E_RENDERER_STARTUP_ERRORS_PROPERTY] = errors
  observedApp[E2E_RENDERER_STARTUP_PENDING_PROPERTY] = pending

  app.on('web-contents-created', (_event, contents) => {
    contents.on('console-message', (_consoleEvent, detailsOrLevel, message) => {
      const details =
        detailsOrLevel && typeof detailsOrLevel === 'object'
          ? (detailsOrLevel as unknown as ConsoleMessageDetails)
          : null

      const level = details ? details.level : detailsOrLevel

      if (level === 3) {
        record('console', details ? details.message : message)
      }
    })
    contents.on('did-fail-load', (_loadEvent, code, description, url, isMainFrame) => {
      if (isMainFrame) {
        record('did-fail-load', `${code} ${description} ${url}`)
      }
    })
    contents.on('render-process-gone', (_goneEvent, details) => {
      record('render-process-gone', `${details.reason} (${details.exitCode})`)
    })

    if (env.HERMES_E2E_INJECT_STARTUP_RENDERER_ERROR === '1') {
      contents.once('dom-ready', () => {
        const injection = contents
          .executeJavaScript(`console.error(${JSON.stringify(E2E_STARTUP_RENDERER_ERROR_SENTINEL)})`)
          .then(() => undefined)
          .catch(error => record('console-injection-failed', error))

        pending.push(injection)
      })
    }
  })
}
