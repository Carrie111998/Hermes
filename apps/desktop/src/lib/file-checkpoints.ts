export interface FileCheckpoint {
  hash: string
  timestamp: string
  message: string
}

export interface FileCheckpointList {
  enabled: boolean
  checkpoints: FileCheckpoint[]
}

/** Studio file revert must leave the conversation intact. */
export const FILE_CHECKPOINT_REWIND_HISTORY = false

export function firstUsePromptStorageKey(profile: string): string {
  const scope = profile.trim() || 'default'
  return `hermes.file-checkpoints.first-use:${scope}`
}

export function hasSeenFileCheckpointFirstUse(profile: string, storage: Pick<Storage, 'getItem'>): boolean {
  return storage.getItem(firstUsePromptStorageKey(profile)) === '1'
}

export function markFileCheckpointFirstUseSeen(profile: string, storage: Pick<Storage, 'setItem'>): void {
  storage.setItem(firstUsePromptStorageKey(profile), '1')
}

export function isCheckpointsEnabledInConfig(config: Record<string, unknown> | null | undefined): boolean {
  const raw = config?.checkpoints
  if (raw === true) {
    return true
  }
  if (!raw || typeof raw !== 'object') {
    return false
  }
  return (raw as { enabled?: unknown }).enabled === true
}

export function withCheckpointsEnabled(config: Record<string, unknown>): Record<string, unknown> {
  const prev =
    config.checkpoints && typeof config.checkpoints === 'object' && !Array.isArray(config.checkpoints)
      ? (config.checkpoints as Record<string, unknown>)
      : {}
  return { ...config, checkpoints: { ...prev, enabled: true } }
}

export function restoreFileCheckpointParams(sessionId: string, hash: string): Record<string, unknown> {
  return {
    hash,
    rewind_history: FILE_CHECKPOINT_REWIND_HISTORY,
    session_id: sessionId
  }
}

export function parseFileCheckpointList(raw: unknown): FileCheckpointList {
  if (!raw || typeof raw !== 'object') {
    return { checkpoints: [], enabled: false }
  }
  const value = raw as { checkpoints?: unknown; enabled?: unknown }
  const rows = Array.isArray(value.checkpoints) ? value.checkpoints : []
  return {
    enabled: value.enabled === true,
    checkpoints: rows
      .filter((row): row is Record<string, unknown> => !!row && typeof row === 'object')
      .map(row => ({
        hash: String(row.hash ?? ''),
        message: String(row.message ?? ''),
        timestamp: String(row.timestamp ?? '')
      }))
      .filter(row => row.hash)
  }
}
