export interface ProfileModelScopeCopy {
  savedForNewSessions: string;
  existingSessionsUnchanged: string;
  scopeUnconfirmed: string;
}

export interface ProfileModelAssignmentScope {
  applies_to?: string;
}

/**
 * Profile model writes are expected to change config defaults only. If an
 * older or incompatible backend omits that contract, do not claim a live
 * model changed (or that the scope is known).
 */
export function describeProfileModelSave(
  model: string,
  result: ProfileModelAssignmentScope,
  copy: ProfileModelScopeCopy,
): string {
  if (result.applies_to !== "new_sessions") {
    return `${copy.scopeUnconfirmed}: ${model}`;
  }
  return `${copy.savedForNewSessions}: ${model}. ${copy.existingSessionsUnchanged}`;
}
