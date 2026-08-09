export interface McpOAuthFlow {
  flow_id: string
  server_name: string
  status: 'starting' | 'authorization_required' | 'approved' | 'error'
  authorization_url: string | null
  error: string | null
  tools?: Array<{ name: string; description: string }>
}

export interface DesktopOAuthFlow {
  flow_id: string
  status: 'starting' | 'authorization_required' | 'approved' | 'error'
  authorization_url: string | null
  error: string | null
}

interface CompleteOptions<T extends DesktopOAuthFlow> {
  serverName: string
  start: (name: string) => Promise<T>
  status: (flowId: string) => Promise<T>
  openExternal: (url: string) => Promise<void>
  sleep?: (milliseconds: number) => Promise<void>
  maxPollFailures?: number
}

interface DirectCompleteOptions<T extends DesktopOAuthFlow> {
  start: () => Promise<T>
  status: (flowId: string) => Promise<T>
  openExternal: (url: string) => Promise<void>
  sleep?: (milliseconds: number) => Promise<void>
  maxPollFailures?: number
}

const defaultSleep = (milliseconds: number) => new Promise<void>(resolve => window.setTimeout(resolve, milliseconds))

export async function completeDesktopOAuth<T extends DesktopOAuthFlow>({
  start,
  status,
  openExternal,
  sleep = defaultSleep,
  maxPollFailures = 3
}: DirectCompleteOptions<T>): Promise<T> {
  const started = await start()

  if (started.status === 'error') {
    throw new Error(started.error || 'OAuth failed to start')
  }

  if (!started.authorization_url) {
    throw new Error('OAuth server did not provide an authorization URL')
  }

  await openExternal(started.authorization_url)

  let pollFailures = 0

  for (;;) {
    let current: T

    try {
      current = await status(started.flow_id)
      pollFailures = 0
    } catch (error) {
      pollFailures += 1

      if (pollFailures >= maxPollFailures) {
        throw error
      }

      await sleep(1000)

      continue
    }

    if (current.status === 'approved') {
      return current
    }

    if (current.status === 'error') {
      throw new Error(current.error || 'OAuth authorization failed')
    }

    await sleep(1000)
  }
}

export async function completeMcpDesktopOAuth({
  serverName,
  start,
  status,
  openExternal,
  sleep = defaultSleep,
  maxPollFailures = 3
}: CompleteOptions<McpOAuthFlow>): Promise<McpOAuthFlow> {
  return completeDesktopOAuth({
    maxPollFailures,
    openExternal,
    sleep,
    start: () => start(serverName),
    status
  })
}
