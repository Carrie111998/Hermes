interface ActiveDrain {
  entryId: string
  ownerId?: number
  scopeKey: string
}

interface DrainHandoffTransaction {
  fromScopeKey: string
  toScopeKey: string
  tokens: number[]
}

export class ComposerQueueDrainArbiter {
  private nextToken = 1
  private readonly activeByToken = new Map<number, ActiveDrain>()
  private readonly tokenByEntry = new Map<string, number>()
  private readonly tokensByScope = new Map<string, Set<number>>()
  private readonly handoffsByTransaction = new Map<string, DrainHandoffTransaction>()
  private readonly reservedScopeCounts = new Map<string, number>()

  private reserveScope(scopeKey: string): void {
    this.reservedScopeCounts.set(scopeKey, (this.reservedScopeCounts.get(scopeKey) ?? 0) + 1)
  }

  private releaseScope(scopeKey: string): void {
    const remaining = (this.reservedScopeCounts.get(scopeKey) ?? 0) - 1

    if (remaining > 0) {
      this.reservedScopeCounts.set(scopeKey, remaining)
    } else {
      this.reservedScopeCounts.delete(scopeKey)
    }
  }

  private entryClaimKey(scopeKey: string, entryId: string): string {
    return `${scopeKey}\0${entryId}`
  }

  begin(scopeKey: string, entryId: string, ownerId?: number): number | null {
    const scope = scopeKey.trim()
    const entry = entryId.trim()

    const entryClaimKey = this.entryClaimKey(scope, entry)

    if (
      !scope ||
      !entry ||
      this.reservedScopeCounts.has(scope) ||
      this.tokensByScope.get(scope)?.size ||
      this.tokenByEntry.has(entryClaimKey)
    ) {
      return null
    }

    const token = this.nextToken++

    this.activeByToken.set(token, { entryId: entry, ...(ownerId === undefined ? {} : { ownerId }), scopeKey: scope })
    this.tokenByEntry.set(entryClaimKey, token)
    this.tokensByScope.set(scope, new Set([token]))

    return token
  }

  excluded(scopeKey: string, entryId: string): boolean {
    const scope = scopeKey.trim()
    const entry = entryId.trim()

    return (
      !scope ||
      !entry ||
      this.reservedScopeCounts.has(scope) ||
      Boolean(this.tokensByScope.get(scope)?.size) ||
      this.tokenByEntry.has(this.entryClaimKey(scope, entry))
    )
  }

  handoff(fromScopeKey: string, toScopeKey: string, transactionId?: string): number {
    const from = fromScopeKey.trim()
    const to = toScopeKey.trim()
    const transaction = transactionId?.trim()
    const existing = transaction ? this.handoffsByTransaction.get(transaction) : undefined

    if (existing) {
      if (existing.fromScopeKey !== from || existing.toScopeKey !== to) {
        throw new Error('Composer drain handoff transaction identity collision')
      }

      return existing.tokens.length
    }

    if (!from || !to) {
      return 0
    }

    const sourceTokens = this.tokensByScope.get(from)
    const transactionTokens = [...(sourceTokens ?? [])]

    if (transaction) {
      this.handoffsByTransaction.set(transaction, {
        fromScopeKey: from,
        toScopeKey: to,
        tokens: transactionTokens
      })
      this.reserveScope(from)
      this.reserveScope(to)
    }

    if (!sourceTokens?.size) {
      return 0
    }

    if (from === to) {
      return sourceTokens.size
    }

    const destinationTokens = this.tokensByScope.get(to) ?? new Set<number>()
    let moved = 0

    this.tokensByScope.set(to, destinationTokens)

    for (const token of sourceTokens) {
      const active = this.activeByToken.get(token)

      if (!active) {
        continue
      }

      const previousEntryClaimKey = this.entryClaimKey(active.scopeKey, active.entryId)

      if (this.tokenByEntry.get(previousEntryClaimKey) === token) {
        this.tokenByEntry.delete(previousEntryClaimKey)
      }

      active.scopeKey = to
      const nextEntryClaimKey = this.entryClaimKey(to, active.entryId)

      if (!this.tokenByEntry.has(nextEntryClaimKey)) {
        this.tokenByEntry.set(nextEntryClaimKey, token)
      }

      destinationTokens.add(token)
      moved += 1
    }

    this.tokensByScope.delete(from)

    return moved
  }

  rollbackHandoff(transactionId: string): number {
    const transactionKey = transactionId.trim()
    const transaction = this.handoffsByTransaction.get(transactionKey)

    if (!transaction) {
      return 0
    }

    this.handoffsByTransaction.delete(transactionKey)
    this.releaseScope(transaction.fromScopeKey)
    this.releaseScope(transaction.toScopeKey)
    const sourceTokens = this.tokensByScope.get(transaction.toScopeKey)
    const destinationTokens = this.tokensByScope.get(transaction.fromScopeKey) ?? new Set<number>()
    let moved = 0

    this.tokensByScope.set(transaction.fromScopeKey, destinationTokens)

    for (const token of transaction.tokens) {
      const active = this.activeByToken.get(token)

      if (!active || active.scopeKey !== transaction.toScopeKey) {
        continue
      }

      const previousEntryClaimKey = this.entryClaimKey(active.scopeKey, active.entryId)

      if (this.tokenByEntry.get(previousEntryClaimKey) === token) {
        this.tokenByEntry.delete(previousEntryClaimKey)
      }

      sourceTokens?.delete(token)
      destinationTokens.add(token)
      active.scopeKey = transaction.fromScopeKey
      const nextEntryClaimKey = this.entryClaimKey(active.scopeKey, active.entryId)

      if (!this.tokenByEntry.has(nextEntryClaimKey)) {
        this.tokenByEntry.set(nextEntryClaimKey, token)
      }

      moved += 1
    }

    if (sourceTokens && !sourceTokens.size) {
      this.tokensByScope.delete(transaction.toScopeKey)
    }

    if (!destinationTokens.size) {
      this.tokensByScope.delete(transaction.fromScopeKey)
    }

    return moved
  }

  finalizeHandoff(transactionId: string): boolean {
    const transactionKey = transactionId.trim()
    const transaction = this.handoffsByTransaction.get(transactionKey)

    if (!transaction) {
      return false
    }

    this.handoffsByTransaction.delete(transactionKey)
    this.releaseScope(transaction.fromScopeKey)
    this.releaseScope(transaction.toScopeKey)

    return true
  }

  finish(token: number, ownerId?: number): string | null {
    const active = this.activeByToken.get(token)

    if (!active || (ownerId !== undefined && active.ownerId !== ownerId)) {
      return null
    }

    this.activeByToken.delete(token)

    const scopeTokens = this.tokensByScope.get(active.scopeKey)

    if (scopeTokens) {
      scopeTokens.delete(token)

      if (!scopeTokens.size) {
        this.tokensByScope.delete(active.scopeKey)
      }
    }

    const entryClaimKey = this.entryClaimKey(active.scopeKey, active.entryId)

    if (this.tokenByEntry.get(entryClaimKey) === token) {
      this.tokenByEntry.delete(entryClaimKey)
    }

    return active.scopeKey
  }

  releaseOwner(ownerId: number): number {
    const ownedTokens = [...this.activeByToken.entries()]
      .filter(([, active]) => active.ownerId === ownerId)
      .map(([token]) => token)

    for (const token of ownedTokens) {
      this.finish(token)
    }

    return ownedTokens.length
  }
}
