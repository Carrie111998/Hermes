import type { HermesGitBaseBranch, HermesGitWorktree, HermesRepoStatus } from '@/global'

interface NewSessionGitBridge {
  baseBranchList: (repoPath: string) => Promise<HermesGitBaseBranch[]>
  repoStatus: (repoPath: string) => Promise<HermesRepoStatus | null>
  worktreeAdd: (
    repoPath: string,
    options?: { name?: string; branch?: string; base?: string; existingBranch?: string }
  ) => Promise<{ path: string; branch: string }>
  worktreeList: (repoPath: string) => Promise<HermesGitWorktree[]>
}

export interface AutomaticSessionWorktree {
  base: string
  branch: string
  path: string
  repoPath: string
}

const isWindowsPath = (value: string): boolean =>
  /^[a-z]:[\\/]/i.test(value.trim()) || /^(?:\\\\|\/\/)[^/\\]+[/\\][^/\\]+/.test(value.trim())

const pathKey = (value: string, foldCase: boolean): string => {
  const normalized = value.trim().replace(/\\/g, '/').replace(/\/+$/, '') || '/'

  return foldCase ? normalized.toLowerCase() : normalized
}

function containingWorktree(cwd: string, worktrees: HermesGitWorktree[]): HermesGitWorktree | null {
  const foldCase = isWindowsPath(cwd)
  const target = pathKey(cwd, foldCase)

  return (
    [...worktrees]
      .sort((a, b) => pathKey(b.path, foldCase).length - pathKey(a.path, foldCase).length)
      .find(worktree => {
        const root = pathKey(worktree.path, foldCase)

        return target === root || (root === '/' ? target.startsWith('/') : target.startsWith(`${root}/`))
      }) ?? null
  )
}

function sessionSlug(id: string): string {
  const safe = id
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)

  return `session-${safe || Date.now().toString(36)}`
}

/**
 * Give a brand-new chat an isolated checkout when its cwd points at a Git
 * repository's main worktree. Explicit linked-worktree targets are preserved,
 * and non-repository folders keep their original cwd.
 */
export async function isolateNewSessionCwd(
  cwd: string,
  git: NewSessionGitBridge,
  idFactory: () => string = () => globalThis.crypto.randomUUID(),
  onCreated?: (worktree: AutomaticSessionWorktree) => void
): Promise<string> {
  const target = cwd.trim()

  const status = target ? await git.repoStatus(target) : null

  if (!target || !status) {
    return target
  }

  const worktrees = await git.worktreeList(target)
  const current = containingWorktree(target, worktrees)

  if (!current?.isMain) {
    return target
  }

  const bases = await git.baseBranchList(target)
  const base = bases.find(candidate => candidate.isDefault && candidate.isRemote) ?? bases.find(candidate => candidate.isDefault)
  const baseName = base?.name ?? status.defaultBranch

  if (!baseName) {
    throw new Error('Cannot isolate a new session without a default branch.')
  }

  const name = sessionSlug(idFactory())

  const result = await git.worktreeAdd(current.path, {
    base: baseName,
    branch: `hermes/${name}`,
    name
  })

  onCreated?.({ base: baseName, branch: result.branch, path: result.path, repoPath: current.path })

  return result.path
}
