interface SessionSectionVisibility {
  hiddenPinnedSessionCount: number
  projectCount: number
  sessionCount: number
  sessionsLoading: boolean
}

export function shouldShowSessionSections({
  hiddenPinnedSessionCount,
  projectCount,
  sessionCount,
  sessionsLoading
}: SessionSectionVisibility): boolean {
  return sessionsLoading || sessionCount > 0 || projectCount > 0 || hiddenPinnedSessionCount > 0
}
