import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export function pathFromScopeInput(targetPath: string): string {
  if (!/^file:/i.test(targetPath)) {
    return targetPath
  }

  return fileURLToPath(targetPath)
}

export function isPathWithinRoots(targetPath: string, roots: readonly string[]): boolean {
  const target = path.resolve(pathFromScopeInput(targetPath))
  return roots.some(root => {
    const resolvedRoot = path.resolve(root)
    return target === resolvedRoot || target.startsWith(`${resolvedRoot}${path.sep}`)
  })
}

export function assertPathWithinRoots(targetPath: string, roots: readonly string[], purpose: string): string {
  if (!isPathWithinRoots(targetPath, roots)) {
    throw new Error(`${purpose} is outside an allowed workspace root`)
  }
  return path.resolve(pathFromScopeInput(targetPath))
}

export function allowedFsRoots(hermesHome: string, activeWorkspace?: string | null): string[] {
  return [hermesHome, activeWorkspace || ''].filter(Boolean).map(root => path.resolve(root))
}

export function assertRealPathWithinRoots(
  targetPath: string,
  roots: readonly string[],
  purpose: string,
  fsImpl: Pick<typeof fs, 'realpathSync' | 'lstatSync'> = fs
): string {
  const lexical = assertPathWithinRoots(pathFromScopeInput(targetPath), roots, purpose)
  let realTarget: string

  try {
    const targetStat = fsImpl.lstatSync(lexical)
    if (targetStat.isSymbolicLink()) {
      throw new Error(`${purpose} does not permit symbolic links`)
    }
    realTarget = fsImpl.realpathSync(lexical)
  } catch (error) {
    if (error instanceof Error && error.message.includes('does not permit symbolic links')) {
      throw error
    }

    // A new path may not exist yet. Resolve the nearest existing ancestor so a
    // symlinked directory cannot smuggle the new path outside the allowlist.
    let ancestor = path.dirname(lexical)
    const suffix: string[] = [path.basename(lexical)]

    while (true) {
      try {
        const ancestorStat = fsImpl.lstatSync(ancestor)
        if (ancestorStat.isSymbolicLink()) {
          throw new Error(`${purpose} does not permit symbolic links`)
        }
        break
      } catch (ancestorError) {
        if (ancestorError instanceof Error && ancestorError.message.includes('does not permit symbolic links')) {
          throw ancestorError
        }
      }

      const parent = path.dirname(ancestor)
      if (parent === ancestor) {
        throw new Error(`${purpose} is outside an allowed workspace root`)
      }
      suffix.unshift(path.basename(ancestor))
      ancestor = parent
    }

    const realAncestor = assertRealPathWithinRoots(ancestor, roots, purpose, fsImpl)
    realTarget = path.join(realAncestor, ...suffix)
  }

  const realRoots = roots.map(root => fsImpl.realpathSync(path.resolve(root)))
  return assertPathWithinRoots(realTarget, realRoots, purpose)
}

export function assertParentPathWithinRoots(
  targetPath: string,
  roots: readonly string[],
  purpose: string,
  fsImpl: Pick<typeof fs, 'realpathSync' | 'lstatSync'> = fs
): string {
  const lexical = assertPathWithinRoots(pathFromScopeInput(targetPath), roots, purpose)
  const parent = path.dirname(lexical)
  const realParent = assertRealPathWithinRoots(parent, roots, purpose, fsImpl)
  return path.join(realParent, path.basename(lexical))
}

export function assertExistingOrParentPathWithinRoots(
  targetPath: string,
  roots: readonly string[],
  purpose: string,
  fsImpl: Pick<typeof fs, 'existsSync' | 'realpathSync' | 'lstatSync'> = fs
): string {
  const lexical = assertPathWithinRoots(pathFromScopeInput(targetPath), roots, purpose)
  try {
    const link = fsImpl.lstatSync(lexical)
    if (link.isSymbolicLink()) {
      throw new Error(`${purpose} does not permit symbolic links`)
    }
  } catch (error) {
    if (error instanceof Error && error.message.includes('does not permit symbolic links')) {
      throw error
    }
  }

  return fsImpl.existsSync(lexical)
    ? assertRealPathWithinRoots(lexical, roots, purpose, fsImpl)
    : assertParentPathWithinRoots(lexical, roots, purpose, fsImpl)
}

export function assertPathInsideHermesOrWorkspace(
  targetPath: string,
  hermesHome: string,
  activeWorkspace: string,
  purpose: string,
  fsImpl: Pick<typeof fs, 'existsSync' | 'realpathSync' | 'lstatSync'> = fs
): string {
  return assertExistingOrParentPathWithinRoots(targetPath, allowedFsRoots(hermesHome, activeWorkspace), purpose, fsImpl)
}
