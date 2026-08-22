import type {
  HermesConnection,
  HermesReadDirResult,
  HermesReadFileTextResult,
  HermesSelectPathsOptions
} from '@/global'
import { $connection } from '@/store/session'

export interface DesktopFsRemotePicker {
  selectPaths: (options?: HermesSelectPathsOptions) => Promise<string[]>
}

let remotePicker: DesktopFsRemotePicker | null = null

export function setDesktopFsRemotePicker(next: DesktopFsRemotePicker | null) {
  remotePicker = next
}

function connectionCacheKey(connection: HermesConnection | null) {
  if (!connection) {
    return 'local:'
  }

  const target =
    connection.remoteKind === 'ssh'
      ? connection.remoteIdentity || connection.remoteHost || ''
      : connection.baseUrl || ''

  return `${connection.mode || 'local'}:${connection.remoteKind || ''}:${connection.profile || ''}:${target}`
}

export function desktopFsCacheKey(connection: HermesConnection | null = $connection.get()) {
  return connectionCacheKey(connection)
}

export function isDesktopFsRemoteMode() {
  return $connection.get()?.mode === 'remote'
}

// Active profile for FS/git REST calls. Without it the Electron api bridge
// hits the primary (local) backend even when the user switched to a remote profile.
export function desktopFsProfile(): string | undefined {
  return $connection.get()?.profile || undefined
}

function fsPath(endpoint: string, filePath: string) {
  return `/api/fs/${endpoint}?path=${encodeURIComponent(filePath)}`
}

function bridge() {
  const desktop = window.hermesDesktop

  if (!desktop) {
    throw new Error('Hermes Desktop bridge is unavailable')
  }

  return desktop
}

function remoteFsApi<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  return bridge().api<T>(
    body ? { body, method: 'POST', path, profile: desktopFsProfile() } : { path, profile: desktopFsProfile() }
  )
}

export async function readDesktopDir(path: string): Promise<HermesReadDirResult> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readDir(path)
  }

  return remoteFsApi<HermesReadDirResult>(fsPath('list', path))
}

export async function readDesktopFileText(path: string): Promise<HermesReadFileTextResult> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readFileText(path)
  }

  return remoteFsApi<HermesReadFileTextResult>(fsPath('read-text', path))
}

export async function createDesktopEntry(parentPath: string, name: string, isDirectory: boolean): Promise<string> {
  const rawParent = String(parentPath || '').trim()
  const rawChild = String(name || '')
  const child = rawChild.trim()
  const windowsBase = child.split('.', 1)[0]?.toUpperCase()
  const windowsReserved = /^(CON|PRN|AUX|NUL|COM(?:[1-9]|[¹²³])|LPT(?:[1-9]|[¹²³]))$/u.test(windowsBase || '')

  if (
    !rawParent ||
    !child ||
    child === '.' ||
    child === '..' ||
    child.includes('/') ||
    child.includes('\\') ||
    child.includes(':') ||
    child.endsWith('.') ||
    rawChild.endsWith('.') ||
    rawChild.endsWith(' ') ||
    windowsReserved
  ) {
    throw new Error('Invalid name')
  }

  const posixRoot = /^\/+$/u.test(rawParent)
  const windowsRoot = /^[a-zA-Z]:[\\/]*$/u.test(rawParent)
  const parent = posixRoot
    ? '/'
    : windowsRoot
      ? `${rawParent.slice(0, 2)}\\`
      : rawParent.replace(/[\\/]+$/, '')
  const separator = parent.includes('\\') && !parent.includes('/') ? '\\' : '/'
  const path = parent.endsWith('/') || parent.endsWith('\\') ? `${parent}${child}` : `${parent}${separator}${child}`

  if (isDesktopFsRemoteMode()) {
    const result = await remoteFsApi<{ ok?: boolean; path?: string }>('/api/fs/create', {
      is_directory: isDirectory,
      name: child,
      parent_path: parent
    })

    return result.path || path
  }

  const desktop = bridge()

  if (!desktop.createEntry) {
    throw new Error('File creation is not available')
  }

  return (await desktop.createEntry(parent, child, isDirectory)).path
}

// Save UTF-8 text back to a file. Local writes go through the hardened Electron
// IPC; remote writes hit the dashboard's POST /api/fs/write-text (same path
// hardening, parent-must-exist, size cap) so the editor behaves identically in
// both modes. Stale-on-disk detection is the caller's job (re-read before save).
export async function writeDesktopFileText(path: string, content: string): Promise<{ path: string }> {
  const desktop = bridge()

  if (!isDesktopFsRemoteMode()) {
    if (!desktop.writeTextFile) {
      throw new Error('Saving is not available')
    }

    return desktop.writeTextFile(path, content)
  }

  const result = await remoteFsApi<{ ok?: boolean; path?: string }>('/api/fs/write-text', { content, path })

  return { path: result.path || path }
}

export async function readDesktopFileDataUrl(path: string): Promise<string> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readFileDataUrl(path)
  }

  const result = await remoteFsApi<string | { dataUrl?: string }>(fsPath('read-data-url', path))

  return typeof result === 'string' ? result : result.dataUrl || ''
}

/**
 * Read a composer image local-shell first, even when the active agent is
 * remote. Picker, clipboard, and OS-drop paths belong to this machine; in-app
 * project-tree paths may belong only to the gateway and fall back there.
 */
export async function readDesktopFileDataUrlLocalFirst(path: string): Promise<string> {
  try {
    const local = await window.hermesDesktop?.readFileDataUrl?.(path)

    if (local) {
      return local
    }
  } catch (error) {
    if (!isDesktopFsRemoteMode()) {
      throw error
    }

    // Not on this machine (or unreadable locally) — try the active gateway.
  }

  return readDesktopFileDataUrl(path)
}

export async function desktopGitRoot(path: string): Promise<string | null> {
  const desktop = bridge()

  if (!isDesktopFsRemoteMode()) {
    return desktop.gitRoot ? desktop.gitRoot(path) : null
  }

  return (await remoteFsApi<{ root: string | null }>(fsPath('git-root', path))).root
}

export async function desktopDefaultCwd(): Promise<{ branch: string; cwd: string } | null> {
  if (!isDesktopFsRemoteMode()) {
    return null
  }

  return remoteFsApi<{ branch: string; cwd: string }>('/api/fs/default-cwd')
}

// Reveal a path in the OS file manager (Finder / Explorer / Files). Local only.
export async function revealDesktopPath(path: string): Promise<void> {
  await bridge().revealPath?.(path)
}

// Rename a file/folder in place; returns the new absolute path. Local only.
export async function renameDesktopPath(path: string, newName: string): Promise<string> {
  const desktop = bridge()

  if (!desktop.renamePath) {
    throw new Error('Rename is not available')
  }

  const result = await desktop.renamePath(path, newName)

  return result.path
}

// Move a file/folder to the OS trash (recoverable). Local only.
export async function trashDesktopPath(path: string): Promise<void> {
  const desktop = bridge()

  if (!desktop.trashPath) {
    throw new Error('Delete is not available')
  }

  await desktop.trashPath(path)
}

export async function copyTextToClipboard(text: string): Promise<void> {
  await bridge().writeClipboard(text)
}

// Working-tree-vs-HEAD diff for one file. Empty when unchanged / not a repo.
// Remote gateway → backend git (/api/git/file-diff); local → Electron git.
export async function desktopFileDiff(repoRoot: string, filePath: string): Promise<string> {
  if (isDesktopFsRemoteMode()) {
    const result = await remoteFsApi<{ diff: string }>(
      `/api/git/file-diff?path=${encodeURIComponent(repoRoot)}&file=${encodeURIComponent(filePath)}`
    )

    return result.diff || ''
  }

  const git = bridge().git

  return git?.fileDiff ? git.fileDiff(repoRoot, filePath) : ''
}

export async function selectDesktopPaths(options?: HermesSelectPathsOptions): Promise<string[]> {
  const desktop = bridge()
  const profile = desktopFsProfile()
  const localOptions = profile ? { ...options, profile } : options

  if (!isDesktopFsRemoteMode()) {
    return desktop.selectPaths(localOptions)
  }

  if (!options?.directories) {
    return desktop.selectPaths(localOptions)
  }

  return remotePicker ? remotePicker.selectPaths({ ...options, multiple: false }) : []
}
