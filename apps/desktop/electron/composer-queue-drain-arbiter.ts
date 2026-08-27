interface ActiveDrain {
  entryId: string
  ownerId?: number
  scopeKey: string
}

export class ComposerQueueDrainArbiter {
  private nextToken = 1
  private readonly activeByToken = new Map<number, ActiveDrain>()
  private readonly tokenByEntry = new Map<string, number>()
  private readonly tokensByScope = new Map<string, Set<number>>()

  private entryClaimKey(scopeKey: string, entryId: string): string {
    return `${scopeKey}\0${entryId}`
  }

  begin(scopeKey: string, entryId: string, ownerId?: number): number | null {
    const scope = scopeKey.trim()
    const entry = entryId.trim()

    const entryClaimKey = this.entryClaimKey(scope, entry)

    if (!scope || !entry || this.tokensByScope.get(scope)?.size || this.tokenByEntry.has(entryClaimKey)) {
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
      Boolean(this.tokensByScope.get(scope)?.size) ||
      this.tokenByEntry.has(this.entryClaimKey(scope, entry))
    )
  }

  handoff(fromScopeKey: string, toScopeKey: string): number {
    const from = fromScopeKey.trim()
    const to = toScopeKey.trim()
    const sourceTokens = this.tokensByScope.get(from)

    if (!from || !to || !sourceTokens?.size) {
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

  finish(token: number): string | null {
    const active = this.activeByToken.get(token)

    if (!active) {
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
