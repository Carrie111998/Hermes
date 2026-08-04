import path from 'node:path'

export const MANAGED_ABOUT_COPYRIGHT =
  'Copyright © 2026 Electric Sheep. Built on Hermes Agent by Nous Research under the MIT License.'

export interface NativeAboutPanelInput {
  applicationName: string
  appVersion: string
  managed: boolean
  upstreamVersion: string
}

export interface NativeAboutPanelOptions {
  applicationName: string
  applicationVersion: string
  copyright: string
}

/** Native About copy is rebuilt each time the panel opens. Keep the managed
 * identity and exact legal attribution on both the seed and refresh paths. */
export function nativeAboutPanelOptions({
  applicationName,
  appVersion,
  managed,
  upstreamVersion
}: NativeAboutPanelInput): NativeAboutPanelOptions {
  return {
    applicationName,
    applicationVersion: managed ? appVersion : upstreamVersion,
    copyright: managed ? MANAGED_ABOUT_COPYRIGHT : 'Copyright © 2026 Nous Research'
  }
}

export interface NativeAppIconInput {
  appRoot: string
  isWindows: boolean
  resourcesPath: string
  unpackedAppRoot: string
}

export function nativeAppIconCandidates({
  appRoot,
  isWindows,
  resourcesPath,
  unpackedAppRoot
}: NativeAppIconInput): string[] {
  return [
    ...(isWindows ? [path.join(resourcesPath, 'eva.ico'), path.join(appRoot, 'assets', 'eva.ico')] : []),
    path.join(appRoot, 'public', 'eva.png'),
    path.join(appRoot, 'dist', 'eva.png'),
    path.join(unpackedAppRoot, 'dist', 'eva.png')
  ]
}
