import path from 'node:path'

export type FirstBootReceipt =
  | {
      ok: true
      stage: 'first-boot'
      httpReady: true
      websocketReady: true
      profileReady: true
      profile: string | null
    }
  | {
      ok: false
      stage: 'first-boot'
      httpReady: boolean
      websocketReady: false
      error: string
    }
  | {
      ok: false
      stage: 'first-boot'
      httpReady: true
      websocketReady: true
      profileReady: false
      error: string
    }

export interface FirstBootReadinessOptions {
  waitForHttp: () => Promise<void>
  probeWebSocket: () => Promise<{ ok: boolean; reason?: string }>
  verifyProfile?: () => Promise<{ ok: boolean; reason?: string; profile?: string | null }>
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Compare profile homes using the target platform's filesystem semantics. */
export function pathsReferToSameLocation(left: string, right: string, isWindows = process.platform === 'win32'): boolean {
  if (isWindows) {
    return path.win32.resolve(left).toLowerCase() === path.win32.resolve(right).toLowerCase()
  }

  return path.resolve(left) === path.resolve(right)
}

/** Require both transports and the selected profile before declaring first boot ready. */
export async function verifyFirstBootReadiness({
  waitForHttp,
  probeWebSocket,
  verifyProfile
}: FirstBootReadinessOptions): Promise<FirstBootReceipt> {
  let verifiedProfile: string | null = null

  try {
    await waitForHttp()
  } catch (error) {
    return {
      ok: false,
      stage: 'first-boot',
      httpReady: false,
      websocketReady: false,
      error: errorMessage(error)
    }
  }

  try {
    const websocket = await probeWebSocket()

    if (!websocket.ok) {
      return {
        ok: false,
        stage: 'first-boot',
        httpReady: true,
        websocketReady: false,
        error: websocket.reason || 'WebSocket readiness probe failed.'
      }
    }
  } catch (error) {
    return {
      ok: false,
      stage: 'first-boot',
      httpReady: true,
      websocketReady: false,
      error: errorMessage(error)
    }
  }

  try {
    const profile = await verifyProfile?.()

    if (profile && !profile.ok) {
      return {
        ok: false,
        stage: 'first-boot',
        httpReady: true,
        websocketReady: true,
        profileReady: false,
        error: profile.reason || 'Active profile verification failed.'
      }
    }

    verifiedProfile = profile?.profile ?? null
  } catch (error) {
    return {
      ok: false,
      stage: 'first-boot',
      httpReady: true,
      websocketReady: true,
      profileReady: false,
      error: errorMessage(error)
    }
  }

  return {
    ok: true,
    stage: 'first-boot',
    httpReady: true,
    websocketReady: true,
    profileReady: true,
    profile: verifiedProfile
  }
}
