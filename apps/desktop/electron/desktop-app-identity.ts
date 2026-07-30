export interface CodesignCommandResult {
  error: Error | null
  stderr: string
  stdout: string
}

export type CodesignRunner = (
  args: string[]
) => Promise<CodesignCommandResult>

export async function resolveMacCodeSigningIdentity(options: {
  bundlePath: string
  isMac: boolean
  isPackaged: boolean
  runCodesign: CodesignRunner
}): Promise<string> {
  if (!options.isPackaged) {
    throw new Error(
      'desktop direct-action provenance is disabled for development builds'
    )
  }

  if (!options.isMac) {
    throw new Error(
      'desktop direct-action provenance requires verified macOS code signing'
    )
  }

  const [verified, described] = await Promise.all([
    options.runCodesign([
      '--verify',
      '--deep',
      '--strict',
      options.bundlePath
    ]),
    options.runCodesign(['-dv', '--verbose=4', options.bundlePath])
  ])

  const details = `${described.stdout}\n${described.stderr}`

  const identifier =
    details.match(/^Identifier=(.+)$/m)?.[1]?.trim() || ''

  const team =
    details.match(/^TeamIdentifier=(.+)$/m)?.[1]?.trim() || ''

  if (
    verified.error ||
    described.error ||
    !identifier ||
    !team ||
    team === 'not set'
  ) {
    throw new Error('desktop application signing identity is unavailable')
  }

  return `${team}:${identifier}`
}

export function cacheAsyncResult<T>(
  load: () => Promise<T>
): () => Promise<T> {
  let pending: Promise<T> | null = null

  return () => {
    pending ??= load()

    return pending
  }
}
