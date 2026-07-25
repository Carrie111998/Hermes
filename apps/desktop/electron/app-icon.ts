import path from 'node:path'

interface ResolveAppIconPathOptions {
  appRoot: string
  fileExists: (filePath: string) => boolean
  isPackaged: boolean
  pathModule?: typeof path
  unpackedAppRoot: string
}

export function resolveAppIconPath({
  appRoot,
  fileExists,
  isPackaged,
  pathModule = path,
  unpackedAppRoot
}: ResolveAppIconPathOptions): string | undefined {
  const candidates = isPackaged
    ? [pathModule.join(unpackedAppRoot, 'dist', 'apple-touch-icon.png')]
    : [
        pathModule.join(appRoot, 'public', 'apple-touch-icon.png'),
        pathModule.join(appRoot, 'dist', 'apple-touch-icon.png')
      ]

  return candidates.find(fileExists)
}
