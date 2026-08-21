interface InstallStampLike {
  branch?: unknown
  source?: unknown
}

/**
 * Local builds produced from a non-main branch carry changes that an official
 * `origin/main` self-update would replace. They stay updateable through their
 * managed fork/rebase workflow, but the generic in-app updater must fail safe.
 */
export function customBuildUpdateBlock(stamp: InstallStampLike | null | undefined): string | null {
  const branch = typeof stamp?.branch === 'string' ? stamp.branch.trim() : ''
  const source = typeof stamp?.source === 'string' ? stamp.source.trim() : ''

  if (source === 'local' && branch && branch !== 'main') {
    return `Custom build on ${branch}: in-app official updates are disabled to preserve local enhancements.`
  }

  return null
}
