export interface BackendIdentity {
  nonce: string
  pid: number
  profile: string
  startMarker: string
}

export interface BackendOwnershipEntry extends BackendIdentity {
  command?: string
  /** PID of the Electron parent that spawned this backend, when known. */
  parentPid?: number
  /** Start marker of that parent, so a reused PID is not mistaken for it. */
  parentStartMarker?: string
}

export interface BackendOwnershipStore {
  read: () => string | null
  write: (contents: string) => void
  /** Serialize every read/merge/write mutation across Electron interpreters. */
  transaction?: <T>(operation: () => T) => T
  /** Move an unreadable ownership file aside (e.g. rename to `.corrupt`) so
   *  its contents survive for inspection instead of being rewritten away.
   *  Optional: stores that can't quarantine simply skip the sweep. */
  quarantine?: () => void
}

export interface BackendOwnershipDeps {
  /**
   * Inspect the whole persisted roster from one process-table snapshot.
   *
   * Windows process discovery is a cold PowerShell/CIM call. Calling the two
   * scalar probes below for every old entry made startup O(roster size) in
   * multi-second shell launches. A batch inspector keeps the persisted
   * pid+creation-time ledger authoritative without paying that cost per row.
   */
  inspect?: (entries: readonly BackendOwnershipEntry[]) => Promise<readonly BackendOwnershipInspection[]>
  matchesIdentity: (identity: BackendIdentity) => Promise<boolean | undefined>
  /** True when the recorded parent is still running; undefined when unknown. */
  matchesParent: (entry: BackendOwnershipEntry) => Promise<boolean | undefined>
  stop: (identity: BackendIdentity) => Promise<void> | void
  store: BackendOwnershipStore
}

export interface BackendOwnershipInspection {
  identityMatches: boolean | undefined
  parentMatches: boolean | undefined
}

export interface BackendClaim extends BackendIdentity {
  command?: string
  parentPid?: number
  parentStartMarker?: string
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0
}

function isCompleteIdentity(value: unknown): value is BackendIdentity {
  if (!value || typeof value !== 'object') {
    return false
  }

  const candidate = value as Partial<BackendIdentity>

  return (
    Number.isInteger(candidate.pid) &&
    Number(candidate.pid) > 0 &&
    isNonEmptyString(candidate.startMarker) &&
    isNonEmptyString(candidate.nonce) &&
    isNonEmptyString(candidate.profile)
  )
}

function identitiesMatch(left: BackendIdentity, right: BackendIdentity): boolean {
  return (
    left.pid === right.pid &&
    left.startMarker === right.startMarker &&
    left.nonce === right.nonce &&
    left.profile === right.profile
  )
}

export function parseBackendOwnership(contents: unknown): BackendOwnershipEntry[] {
  return parseBackendOwnershipDetailed(contents).entries
}

/** Parse result that distinguishes "empty/valid" from "unreadable". A corrupt
 *  ownership file must NOT read as an empty roster: `reapOrphans` rewrites the
 *  file with its survivors, so treating garbage as `[]` permanently erased the
 *  records of still-running backends — the exact shape of the #89298 report
 *  (ownership file gone, 28 leaked serve processes nothing will ever reap). */
export function parseBackendOwnershipDetailed(contents: unknown): {
  corrupt: boolean
  entries: BackendOwnershipEntry[]
} {
  const text = String(contents ?? '')

  if (!text.trim()) {
    return { corrupt: false, entries: [] }
  }

  let parsed: unknown

  try {
    parsed = JSON.parse(text)
  } catch {
    return { corrupt: true, entries: [] }
  }

  const values = Array.isArray(parsed)
    ? parsed
    : parsed && typeof parsed === 'object' && Array.isArray((parsed as { backends?: unknown }).backends)
      ? (parsed as { backends: unknown[] }).backends
      : []

  const entries: BackendOwnershipEntry[] = []

  for (const value of values) {
    if (!isCompleteIdentity(value)) {
      continue
    }

    const candidate = value as BackendOwnershipEntry

    const entry: BackendOwnershipEntry = {
      nonce: candidate.nonce,
      pid: candidate.pid,
      profile: candidate.profile,
      startMarker: candidate.startMarker
    }

    if (typeof candidate.command === 'string') {
      entry.command = candidate.command
    }

    if (Number.isInteger(candidate.parentPid) && Number(candidate.parentPid) > 0) {
      entry.parentPid = candidate.parentPid
    }

    if (isNonEmptyString(candidate.parentStartMarker)) {
      entry.parentStartMarker = candidate.parentStartMarker
    }

    if (!entries.some(existing => identitiesMatch(existing, entry))) {
      entries.push(entry)
    }
  }

  return { corrupt: false, entries }
}

export function serializeBackendOwnership(entries: BackendOwnershipEntry[]): string {
  return `${JSON.stringify({ backends: entries }, null, 2)}\n`
}

/**
 * Persistent ownership for local backend roots.
 *
 * Claiming is asynchronous so a failed persistence transaction can await child
 * cleanup before reporting failure to the caller.
 */
export function createBackendOwnership(deps: BackendOwnershipDeps) {
  const readDetailed = (): {
    corrupt: boolean
    unavailable: boolean
    entries: BackendOwnershipEntry[]
  } => {
    try {
      const parsed = parseBackendOwnershipDetailed(deps.store.read())
      return { ...parsed, unavailable: false }
    } catch {
      // A read failure is not an empty roster. Callers that are allowed to
      // continue must quarantine the file and preserve all live processes.
      return { corrupt: false, unavailable: true, entries: [] }
    }
  }
  const read = () => readDetailed().entries
  const readForMutation = () => {
    const snapshot = readDetailed()
    if (snapshot.corrupt || snapshot.unavailable) {
      try {
        deps.store.quarantine?.()
      } catch {
        // Preserve the original failure; never continue with an empty roster.
      }
      throw new Error('Backend ownership store is unreadable or corrupt; mutation refused.')
    }
    return snapshot.entries
  }
  const write = (entries: BackendOwnershipEntry[]) => deps.store.write(serializeBackendOwnership(entries))
  const transaction = <T>(operation: () => T): T => deps.store.transaction ? deps.store.transaction(operation) : operation()

  const inspectBatch = async (
    entries: readonly BackendOwnershipEntry[],
    { identityOnly = false }: { identityOnly?: boolean } = {}
  ): Promise<BackendOwnershipInspection[]> => {
    if (!entries.length) {
      return []
    }

    if (deps.inspect) {
      try {
        const inspected = await deps.inspect(entries)

        return entries.map((_, index) => ({
          identityMatches: inspected[index]?.identityMatches,
          parentMatches: inspected[index]?.parentMatches
        }))
      } catch {
        // A process-table failure is uncertainty, never proof that a process
        // is dead. Preserve every record so a later launch can retry.
        return entries.map(() => ({ identityMatches: undefined, parentMatches: undefined }))
      }
    }

    // Non-Windows fallback: overlap independent probes so roster size does
    // not turn into a serial startup delay even when no batch API exists.
    return Promise.all(
      entries.map(async entry => {
        let parentMatches: boolean | undefined

        if (!identityOnly) {
          try {
            parentMatches = await deps.matchesParent(entry)
          } catch {
            return { identityMatches: undefined, parentMatches: undefined }
          }

          if (parentMatches === true) {
            return { identityMatches: undefined, parentMatches }
          }
        }

        try {
          return {
            identityMatches: await deps.matchesIdentity(entry),
            parentMatches
          }
        } catch {
          return { identityMatches: undefined, parentMatches }
        }
      })
    )
  }

  let claimCompaction: Promise<number> | null = null

  const compactStaleEntries = async (): Promise<number> => {
    const snapshot = readDetailed()

    if (snapshot.corrupt || snapshot.unavailable || !snapshot.entries.length) {
      return 0
    }

    const inspected = await inspectBatch(snapshot.entries, { identityOnly: true })
    const stale = snapshot.entries.filter((_entry, index) => inspected[index]?.identityMatches === false)

    if (!stale.length) {
      return 0
    }

    // The snapshot probe awaits. Merge confirmed-stale removals into a fresh
    // roster so concurrent claims survive and concurrent releases are not
    // resurrected. Exact identity matching protects PID reuse.
    const compacted = transaction(() => {
      const current = readDetailed()

      if (current.corrupt || current.unavailable) {
        return 0
      }

      const next = current.entries.filter(entry => !stale.some(candidate => identitiesMatch(candidate, entry)))

      if (next.length !== current.entries.length) {
        write(next)
      }

      return current.entries.length - next.length
    })

    return compacted
  }

  const compactStale = (): Promise<number> => {
    if (!claimCompaction) {
      claimCompaction = compactStaleEntries().finally(() => {
        claimCompaction = null
      })
    }

    return claimCompaction
  }

  return {
    async claim(claim: BackendClaim): Promise<BackendOwnershipEntry> {
      if (!isCompleteIdentity(claim)) {
        throw new Error('Cannot own a backend without a complete process identity.')
      }

      const entry: BackendOwnershipEntry = {
        nonce: claim.nonce,
        pid: claim.pid,
        profile: claim.profile,
        startMarker: claim.startMarker
      }

      if (typeof claim.command === 'string') {
        entry.command = claim.command
      }

      if (Number.isInteger(claim.parentPid) && Number(claim.parentPid) > 0) {
        entry.parentPid = claim.parentPid
      }

      if (isNonEmptyString(claim.parentStartMarker)) {
        entry.parentStartMarker = claim.parentStartMarker
      }

      try {
        // Claim is deliberately one synchronous read/merge/write transaction.
        // In particular, never put a process-table probe between read and
        // write: Windows pays a multi-second PowerShell cold start for that
        // probe, and two claims awaiting it can both merge from the same stale
        // roster then overwrite one another. Persist this exact identity
        // first; bounded maintenance, when needed, runs after the write and
        // fresh-merges only confirmed-stale removals.
        transaction(() => {
          const entries = readForMutation().filter(candidate => candidate.pid !== entry.pid)
          const next = [...entries, entry]
          write(next)
        })

        // Prune on every write (#92875), but never put the cold Windows
        // process-table snapshot on the claim's critical path. The
        // single-flight compactor classifies the whole roster in one batch and
        // fresh-merges confirmed-stale removals after this claim has resolved.
        void compactStale().catch(() => {
          // Compaction is maintenance; the persisted exact claim remains
          // valid and startup reap will retry on the next launch.
        })
      } catch (error) {
        try {
          await deps.stop(entry)
        } catch {
          // Persistence remains the claim failure even if cleanup also fails.
        }

        throw error
      }

      return entry
    },

    compactStale,

    release(identity: BackendIdentity): void {
      if (!isCompleteIdentity(identity)) {
        throw new Error('Cannot release a backend without a complete process identity.')
      }

      transaction(() => {
        const entries = readForMutation()
        const next = entries.filter(entry => !identitiesMatch(entry, identity))

        if (next.length !== entries.length) {
          write(next)
        }
      })
    },

    async reapOrphans(): Promise<number[]> {
      const { corrupt, unavailable, entries } = readDetailed()

      // An unreadable ownership file yields zero parsed entries — rewriting
      // survivors ([]) here would DESTROY the only record of any backends the
      // corrupt file described, guaranteeing they leak forever (#89298).
      // Preserve the evidence for inspection and skip the sweep.
      if (corrupt || unavailable) {
        try {
          deps.store.quarantine?.()
        } catch {
          // Quarantine is best-effort; the important part is not rewriting.
        }

        return []
      }

      const removed: BackendOwnershipEntry[] = []
      const reaped: number[] = []
      const inspected = await inspectBatch(entries)

      for (const [index, entry] of entries.entries()) {
        // A backend whose Electron parent is still running is NOT an orphan:
        // reaping it would kill a live instance's session. This is what stops
        // a second launch from SIGTERMing the running instance's backend even
        // if it reaches reapOrphans (see main.ts startHermes + #87295).
        const parentAlive = inspected[index]?.parentMatches

        if (parentAlive === true) {
          continue
        }

        const matches = inspected[index]?.identityMatches

        if (matches === false) {
          removed.push(entry)
          continue
        }

        if (matches !== true) {
          continue
        }

        try {
          await deps.stop(entry)
          reaped.push(entry.pid)
          removed.push(entry)
        } catch {
          // Preserve failed ownership so a later startup can retry it.
        }
      }

      // Inspection and process teardown await. Merge the removals into a fresh
      // roster instead of rewriting the pre-await snapshot: a backend claimed
      // meanwhile must survive, and an entry released meanwhile must not be
      // resurrected. Exact pid+start+nonce+profile matching also protects a
      // newly reused PID from an old snapshot's removal decision.
      transaction(() => {
        const current = readDetailed()

        if (current.corrupt || current.unavailable) {
          try {
            deps.store.quarantine?.()
          } catch {
            // Preserve rather than overwrite concurrent unreadable evidence.
          }

          return
        }

        write(current.entries.filter(entry => !removed.some(candidate => identitiesMatch(candidate, entry))))
      })

      return reaped
    },

    clear(): void {
      transaction(() => {
        readForMutation()
        write([])
      })
    }
  }
}

export function backendCommandMatches(command: unknown): boolean {
  return /(?:^|[\s/\\"])(?:hermes(?:\.exe)?|hermes_cli\.main|hermes_cli[/\\]main\.py)"?(?:\s+(?:--profile|-p)\s+\S+)?\s+(?:serve|dashboard)(?:\s|$)/i.test(
    String(command ?? '')
  )
}

/** Coordinates all quit paths so asynchronous backend teardown runs once. */
export function createBackendShutdownCoordinator(teardown: () => Promise<void> | void) {
  let completion: Promise<void> | undefined

  return {
    run(): Promise<void> {
      if (!completion) {
        completion = Promise.resolve().then(teardown)
      }

      return completion
    },
    hasStarted(): boolean {
      return completion !== undefined
    }
  }
}
