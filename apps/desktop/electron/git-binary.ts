import path from 'node:path'

export interface GitBinaryOptions {
  isWindows: boolean
  env: Record<string, string | undefined>
  fileExists: (filePath: string) => boolean
  findOnPath?: (command: string) => string | null
}

/**
 * Locate the git executable.
 *
 * Resolution order (first match wins), mirroring resolveGitBinary() in main.ts:
 *   1. PortableGit under %LOCALAPPDATA%\hermes\git\ (install.ps1 / self-managed)
 *   2. Standard Git for Windows install locations (ProgramFiles, ProgramFiles(x86))
 *   3. %LOCALAPPDATA%\Programs\Git\ (user-scoped)
 *   4. git on PATH
 *
 * Windows-specific hardening: simple-git's `customBinaryPlugin` validates the
 * git binary path with a regex that rejects spaces (and other "restricted"
 * characters). When the resolved git lives under "Program Files" the path
 * contains a space, and simple-git emits a noisy
 * `Invalid value supplied for custom binary, restricted characters must be
 * removed or supply the unsafe.allowUnsafeCustomBinary option` warning on every
 * spawn — even though the call still works (allowUnsafeCustomBinary is set).
 *
 * To keep the console clean we prefer, in order:
 *   - a known-install candidate whose path has NO spaces, or
 *   - the bare name "git" (which simple-git treats as space-free and resolves
 *     via the process PATH), when `git` is discoverable on PATH, or
 *   - the first existing candidate (even with spaces) as a last resort, so a
 *     machine whose sole git is Git for Windows under Program Files still works.
 */
export function resolveGitBinary(opts: GitBinaryOptions): string {
  const { isWindows, env, fileExists, findOnPath } = opts

  if (!isWindows) {
    return findOnPath ? findOnPath('git') || 'git' : 'git'
  }

  const localAppData = env.LOCALAPPDATA || ''
  const joinWin = path.win32.join

  const candidates: string[] = []

  if (localAppData) {
    candidates.push(joinWin(localAppData, 'hermes', 'git', 'cmd', 'git.exe'))
    candidates.push(joinWin(localAppData, 'hermes', 'git', 'bin', 'git.exe'))
  }

  candidates.push(joinWin(env['ProgramFiles'] || 'C:\\Program Files', 'Git', 'cmd', 'git.exe'))
  candidates.push(joinWin(env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Git', 'cmd', 'git.exe'))

  if (localAppData) {
    candidates.push(joinWin(localAppData, 'Programs', 'Git', 'cmd', 'git.exe'))
  }

  const existing = candidates.filter(fileExists)

  // Prefer a known-install candidate whose path has no spaces.
  const withoutSpaces = existing.find(c => !c.includes(' '))

  if (withoutSpaces) {
    return withoutSpaces
  }

  // No space-free known-install git, but `git` resolves on PATH: return the bare
  // name so simple-git sees a space-free binary and stays quiet. The spawned
  // `git` uses the process PATH, which is where findOnPath just found it.
  if (findOnPath && findOnPath('git')) {
    return 'git'
  }

  // Fall back to the first existing candidate (may contain spaces) so a machine
  // whose only git is under Program Files still functions — at the cost of the
  // cosmetic warning.
  if (existing.length > 0) {
    return existing[0]
  }

  return 'git'
}
