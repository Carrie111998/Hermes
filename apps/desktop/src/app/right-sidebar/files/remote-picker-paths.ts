export interface RemotePathCrumb {
  label: string
  path: string
}

function isWindowsPath(path: string): boolean {
  return path.includes('\\') && !path.startsWith('/')
}

export function cleanRemotePath(path: string): string {
  const value = String(path || '')
  const cleaned = value.replace(/[/\\]+$/, '')

  if (cleaned) {
    return cleaned
  }

  return isWindowsPath(value) ? '\\' : '/'
}

export function parentRemotePath(path: string): string {
  const value = cleanRemotePath(path)

  if (/^[A-Za-z]:$/.test(value)) {
    return `${value}\\`
  }

  const separatorIndex = Math.max(value.lastIndexOf('/'), value.lastIndexOf('\\'))

  if (separatorIndex < 0) {
    return value
  }

  if (separatorIndex === 0) {
    return '/'
  }

  const parent = value.slice(0, separatorIndex)

  return /^[A-Za-z]:$/.test(parent) ? `${parent}\\` : parent
}

export function remotePathLeaf(path: string): string {
  return cleanRemotePath(path).split(/[/\\]/).filter(Boolean).pop() || path
}

export function remotePathCrumbs(path: string): RemotePathCrumb[] {
  const value = cleanRemotePath(path)
  const windows = isWindowsPath(value)
  const parts = value.split(/[/\\]/).filter(Boolean)
  const crumbs: RemotePathCrumb[] = windows ? [] : [{ label: '/', path: '/' }]
  let current = ''

  for (const [index, part] of parts.entries()) {
    current = index === 0 ? (windows ? part : `/${part}`) : `${current}${windows ? '\\' : '/'}${part}`
    crumbs.push({ label: part, path: windows && index === 0 ? `${part}\\` : current })
  }

  return crumbs
}
