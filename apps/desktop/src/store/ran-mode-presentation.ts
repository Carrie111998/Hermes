export interface ToolDiffOpenPolicy {
  hasInlineDiff: boolean
  isError: boolean
  isPending: boolean
  ranModeEnabled: boolean
}

/**
 * Ran Mode hides settled-success noise only. Live work and failures keep the
 * same visible disclosure behavior as the normal Desktop experience.
 */
export function shouldAutoOpenToolDiff({
  hasInlineDiff,
  isError,
  isPending,
  ranModeEnabled
}: ToolDiffOpenPolicy): boolean {
  if (!hasInlineDiff) {
    return false
  }

  return !ranModeEnabled || isPending || isError
}

export function shouldShowPinnedSection({
  pinnedCount,
  ranModeEnabled
}: {
  pinnedCount: number
  ranModeEnabled: boolean
}): boolean {
  return !ranModeEnabled || pinnedCount > 0
}

export function ranModeStatusStackMaxClass(ranModeEnabled: boolean): 'max-h-[30vh]' | 'max-h-[40vh]' {
  return ranModeEnabled ? 'max-h-[30vh]' : 'max-h-[40vh]'
}
