export function deleteBackendPoolEntryIfCurrent<TKey, TEntry>(
  entries: Map<TKey, TEntry>,
  key: TKey,
  expectedEntry: TEntry
): boolean {
  if (entries.get(key) !== expectedEntry) {
    return false
  }

  return entries.delete(key)
}

export function cleanupFailedBackendPoolEntry<TKey, TEntry extends { process: unknown }>(
  entries: Map<TKey, TEntry>,
  key: TKey,
  failedEntry: TEntry,
  stopProcess: (process: TEntry['process']) => void
): void {
  deleteBackendPoolEntryIfCurrent(entries, key, failedEntry)
  stopProcess(failedEntry.process)
}
