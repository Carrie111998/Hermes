const METHOD_NOT_FOUND = -32601

export const RELAY_GLOBAL_CONCURRENCY = 4
export const RELAY_TARGET_CONCURRENCY = 1
export const RELAY_ROSTER_INTERVAL_MS = 60_000
export const RELAY_DRAIN_INTERVAL_MS = 4_000
export const RELAY_LEASE_SECONDS = 180
export const RELAY_RENEW_INTERVAL_MS = 60_000
// Target RPC wraps a subprocess capped at 600s. Leave room for gateway-pool
// queueing and durable receipt commit instead of inheriting the SDK's 30s
// interactive default.
export const RELAY_DELIVERY_REQUEST_TIMEOUT_MS = 720_000
export const RELAY_LEADERSHIP_RETRY_MS = 2_000
export const RELAY_STOP_TIMEOUT_MS = 30_000

const COURIER_NAMESPACE_RE = /^[a-zA-Z0-9_-]{8,160}$/

const text = value => String(value ?? '').trim()

export function createRelayRuntimeId(prefix = 'relay') {
  const uuid = globalThis.crypto?.randomUUID?.()

  if (uuid) {
    return `${prefix}-${uuid}`
  }

  // Chromium and supported Node versions expose crypto.randomUUID(). This is
  // only a fail-closed compatibility fallback for stripped plugin harnesses.
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function validCourierNamespace(value) {
  const candidate = text(value)

  return COURIER_NAMESPACE_RE.test(candidate) ? candidate : null
}

export function isMethodNotFound(error) {
  if (Number(error?.code) === METHOD_NOT_FOUND) {
    return true
  }

  const message = text(error?.message || error).toLowerCase()

  return message.includes('-32601') || message.includes('method not found') || message.includes('unknown rpc method')
}

function errorCode(error) {
  const code = Number(error?.code)

  return Number.isFinite(code) ? code : null
}

function isRetryableDeliveryError(error) {
  const code = errorCode(error)

  // The relay gateway uses 4xxx for bad input, unknown profiles, and replay
  // conflicts. Retrying those under the same immutable envelope cannot help.
  return !(code !== null && code >= 4000 && code < 5000)
}

export function retryDelaySeconds(envelope) {
  const rawAttempt = Number(envelope?.attempt ?? envelope?.attempts ?? 1)
  const attempt = Number.isFinite(rawAttempt) ? Math.max(1, Math.floor(rawAttempt)) : 1

  return Math.min(300, 5 * 2 ** Math.min(attempt - 1, 6))
}

function installId(route) {
  return text(route?.installId || route?.install_id) || null
}

export function relayTargetKey(envelope) {
  return [
    text(envelope?.courier_namespace_id),
    text(envelope?.target_install_id),
    text(envelope?.target_connection),
    text(envelope?.target_profile)
  ].join('\u0000')
}

function leaseFields(envelope, fallback = {}) {
  return {
    token: text(envelope?.lease_token || envelope?.token || fallback?.lease_token || fallback?.token),
    generation:
      envelope?.lease_generation ?? envelope?.generation ?? fallback?.lease_generation ?? fallback?.generation,
    owner: text(envelope?.lease_owner || envelope?.owner || fallback?.lease_owner || fallback?.owner)
  }
}

async function sha256Hex(value) {
  const subtle = globalThis.crypto?.subtle

  if (!subtle) {
    throw new Error('Web Crypto SHA-256 is unavailable')
  }

  const bytes = new TextEncoder().encode(value)
  const digest = await subtle.digest('SHA-256', bytes)

  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('')
}

class KeyedExecutor {
  constructor(limit) {
    this.limit = limit
    this.active = 0
    this.activeKeys = new Set()
    this.queue = []
  }

  run(key, operation) {
    return new Promise((resolve, reject) => {
      this.queue.push({ key, operation, reject, resolve })
      this.pump()
    })
  }

  get freeSlots() {
    return Math.max(0, this.limit - this.active - this.queue.length)
  }

  pump() {
    while (this.active < this.limit) {
      const index = this.queue.findIndex(item => !this.activeKeys.has(item.key))

      if (index < 0) {
        return
      }

      const [item] = this.queue.splice(index, 1)
      this.active += 1
      this.activeKeys.add(item.key)

      Promise.resolve()
        .then(item.operation)
        .then(item.resolve, item.reject)
        .finally(() => {
          this.active -= 1
          this.activeKeys.delete(item.key)
          this.pump()
        })
    }
  }
}

/**
 * Cross-connection Bot Mode courier. The worker owns no credentials: every
 * operation rides a credential-free Desktop route through host.requestProfile.
 */
export function createBotRelayWorker({
  host,
  courierNamespaceId,
  courierId = createRelayRuntimeId('courier'),
  resolveHandle = name => (name === 'default' ? 'hermes' : name),
  onError = () => undefined,
  setIntervalFn = globalThis.setInterval?.bind(globalThis),
  clearIntervalFn = globalThis.clearInterval?.bind(globalThis),
  setTimeoutFn = globalThis.setTimeout?.bind(globalThis),
  clearTimeoutFn = globalThis.clearTimeout?.bind(globalThis),
  rosterIntervalMs = RELAY_ROSTER_INTERVAL_MS,
  drainIntervalMs = RELAY_DRAIN_INTERVAL_MS,
  leaseSeconds = RELAY_LEASE_SECONDS,
  renewIntervalMs = RELAY_RENEW_INTERVAL_MS,
  stopTimeoutMs = RELAY_STOP_TIMEOUT_MS
}) {
  if (!host || typeof host.requestProfile !== 'function') {
    throw new TypeError('bot relay requires host.requestProfile')
  }

  const namespace = text(courierNamespaceId)

  if (!namespace) {
    throw new TypeError('bot relay requires courierNamespaceId')
  }

  const executor = new KeyedExecutor(RELAY_GLOBAL_CONCURRENCY)
  const v1UntilByConnection = new Map()
  const heartbeatTimers = new Set()
  const leasedTasks = new Map()
  let disposed = false
  let rosterTimer = null
  let drainTimer = null
  let rosterBusy = false
  let drainBusy = false
  let claimCursor = 0
  let stopPromise = null

  const report = (error, context) => {
    try {
      onError(error, context)
    } catch {
      // Diagnostics must never become relay authority.
    }
  }

  async function connections() {
    if (disposed || typeof host.profileRoutes !== 'function') {
      return []
    }

    try {
      const routes = await host.profileRoutes()
      const byConnection = new Map()

      for (const route of Array.isArray(routes) ? routes : []) {
        const id = text(route?.connectionId)

        if (id && !byConnection.has(id)) {
          byConnection.set(id, route)
        }
      }

      return [...byConnection.entries()].map(([id, route]) => ({ id, route }))
    } catch (error) {
      report(error, 'connections')
      return []
    }
  }

  async function agentsOn(connection) {
    try {
      const result = await host.requestProfile(connection.route, 'profiles.list', {
        include_sessions: false
      })
      const profiles = Array.isArray(result?.profiles) ? result.profiles : []
      const label = text(connection.route?.connectionLabel || connection.route?.label || connection.id)
      const targetInstallId = installId(connection.route)

      return profiles
        .map(profile => ({
          profile: text(profile?.name),
          handle: resolveHandle(profile?.name, profile),
          connection_id: connection.id,
          connection_label: label,
          courier_namespace_id: namespace,
          ...(targetInstallId ? { target_install_id: targetInstallId } : {}),
          title: text(profile?.ui_meta?.['hermes-bots']?.title || profile?.display_name),
          description: text(profile?.description)
        }))
        .filter(row => row.profile)
    } catch (error) {
      report(error, `profiles:${connection.id}`)
      return []
    }
  }

  async function syncRosters() {
    if (disposed || rosterBusy) {
      return
    }

    rosterBusy = true

    try {
      const current = await connections()

      if (!current.length) {
        return
      }

      const agentsByConnection = new Map()
      await Promise.all(
        current.map(async connection => {
          agentsByConnection.set(connection.id, await agentsOn(connection))
        })
      )

      await Promise.all(
        current.map(async connection => {
          const agents = []

          for (const [id, rows] of agentsByConnection) {
            if (id !== connection.id) {
              agents.push(...rows)
            }
          }

          const sourceInstallId = installId(connection.route)

          try {
            await host.requestProfile(connection.route, 'bot_relay.roster.sync', {
              agents,
              courier_namespace_id: namespace,
              ...(sourceInstallId ? { source_install_id: sourceInstallId } : {})
            })
          } catch (error) {
            // Roster sync is additive; an older gateway simply has no relay door.
            report(error, `roster:${connection.id}`)
          }
        })
      )
    } finally {
      rosterBusy = false
    }
  }

  async function v1Drain(sender) {
    const result = await host.requestProfile(sender.route, 'bot_relay.outbox.drain', {})

    return {
      protocol: 1,
      envelopes: Array.isArray(result?.envelopes) ? result.envelopes : []
    }
  }

  async function claim(sender, limit = 1) {
    if ((v1UntilByConnection.get(sender.id) || 0) > Date.now()) {
      return v1Drain(sender)
    }

    try {
      const result = await host.requestProfile(sender.route, 'bot_relay.outbox.claim', {
        courier_id: courierId,
        courier_namespace_id: namespace,
        lease_seconds: leaseSeconds,
        limit
      })
      const fallback = {
        lease_owner: result?.lease_owner,
        lease_token: result?.lease_token,
        lease_generation: result?.lease_generation
      }

      return {
        protocol: 2,
        envelopes: (Array.isArray(result?.envelopes) ? result.envelopes : []).map(envelope => ({
          ...envelope,
          lease_owner: envelope?.lease_owner ?? fallback.lease_owner,
          lease_token: envelope?.lease_token ?? fallback.lease_token,
          lease_generation: envelope?.lease_generation ?? fallback.lease_generation
        }))
      }
    } catch (error) {
      if (!isMethodNotFound(error)) {
        throw error
      }

      // Cache briefly to avoid a -32601 every four seconds, then probe again
      // so a gateway upgraded while Desktop stays open adopts v2 naturally.
      v1UntilByConnection.set(sender.id, Date.now() + rosterIntervalMs)
      return v1Drain(sender)
    }
  }

  function ownsLease(envelope) {
    const lease = leaseFields(envelope)

    return (
      text(envelope?.courier_namespace_id) === namespace &&
      lease.owner === courierId &&
      Boolean(lease.token) &&
      lease.generation !== undefined &&
      lease.generation !== null
    )
  }

  function startHeartbeat(sender, envelope) {
    let lease = leaseFields(envelope)
    let lost = !ownsLease(envelope)
    let renewing = false
    let timer = null

    const stop = () => {
      if (timer !== null && clearIntervalFn) {
        clearIntervalFn(timer)
        heartbeatTimers.delete(timer)
        timer = null
      }
    }

    const renew = async () => {
      if (lost || renewing) {
        return
      }

      renewing = true

      try {
        const result = await host.requestProfile(sender.route, 'bot_relay.outbox.renew', {
          id: text(envelope?.id),
          courier_id: courierId,
          courier_namespace_id: namespace,
          lease_token: lease.token,
          lease_generation: lease.generation,
          lease_seconds: leaseSeconds
        })
        const next = leaseFields(result, lease)

        if (next.owner && next.owner !== courierId) {
          throw new Error('relay lease owner changed during renewal')
        }

        if (text(result?.courier_namespace_id) && text(result?.courier_namespace_id) !== namespace) {
          throw new Error('relay lease namespace changed during renewal')
        }

        lease = { ...next, owner: next.owner || courierId }
      } catch (error) {
        lost = true
        stop()
        report(error, `renew:${text(envelope?.id)}`)
      } finally {
        renewing = false
      }
    }

    if (!lost && setIntervalFn && clearIntervalFn) {
      timer = setIntervalFn(() => void renew(), renewIntervalMs)
      heartbeatTimers.add(timer)
    }

    return {
      get lost() {
        return lost
      },
      get lease() {
        return lease
      },
      stop
    }
  }

  function casParams(envelope, heartbeat) {
    return {
      id: text(envelope?.id),
      courier_id: courierId,
      courier_namespace_id: namespace,
      lease_token: heartbeat.lease.token,
      lease_generation: heartbeat.lease.generation
    }
  }

  async function nack(sender, envelope, heartbeat, error, retryable, retryAfterSeconds) {
    if (heartbeat.lost) {
      return
    }

    const suppliedDelay = Number(retryAfterSeconds)
    const delay = Number.isFinite(suppliedDelay)
      ? Math.max(0, Math.min(suppliedDelay, 300))
      : retryable
        ? retryDelaySeconds(envelope)
        : null

    try {
      await host.requestProfile(sender.route, 'bot_relay.outbox.nack', {
        ...casParams(envelope, heartbeat),
        error: text(error?.message || error || 'delivery failed'),
        retryable: Boolean(retryable),
        ...(delay !== null ? { retry_after_seconds: delay } : {})
      })
    } catch (nackError) {
      report(nackError, `nack:${text(envelope?.id)}`)
    }
  }

  async function targetDurability(target, capabilitiesByTarget) {
    const key = `${target.id}\u0000${installId(target.route) || ''}`

    if (!capabilitiesByTarget.has(key)) {
      capabilitiesByTarget.set(
        key,
        host
          .requestProfile(target.route, 'bot_relay.capabilities', {})
          .then(result => {
            const supported =
              Number(result?.protocol_version) === 2 && result?.durable_inbox === true

            return {
              retryable: !supported,
              supported
            }
          })
          .catch(error => {
            const unsupportedByTarget = isMethodNotFound(error)

            if (!unsupportedByTarget) {
              report(error, `capabilities:${target.id}`)
            }

            return {
              error: unsupportedByTarget ? null : error,
              retryable: true,
              supported: false
            }
          })
      )
    }

    return capabilitiesByTarget.get(key)
  }

  async function processV2(sender, envelope, byId, capabilitiesByTarget, heartbeat) {
    if (!ownsLease(envelope) || heartbeat.lost) {
      report(new Error('relay returned an envelope not owned by this courier'), 'claim-owner')
      return
    }

    const target = byId.get(text(envelope?.target_connection))

    if (!target) {
      await nack(
        sender,
        envelope,
        heartbeat,
        `connection '${envelope?.target_connection}' is not connected to this Desktop right now`,
        true
      )
      return
    }

    const expectedInstallId = text(envelope?.target_install_id)
    const actualInstallId = installId(target.route)

    if (expectedInstallId && expectedInstallId !== actualInstallId) {
      await nack(
        sender,
        envelope,
        heartbeat,
        `target install changed for connection '${envelope?.target_connection}'`,
        false
      )
      return
    }

    const durability = await targetDurability(target, capabilitiesByTarget)

    if (heartbeat.lost) {
      return
    }

    if (!durability.supported) {
      await nack(
        sender,
        envelope,
        heartbeat,
        durability.error || 'target gateway does not support relay-v2 durable inbox admission',
        durability.retryable
      )
      return
    }

    let result
    const sourceInstallId = installId(sender.route) || text(envelope?.source_install_id)

    try {
      if (heartbeat.lost) {
        return
      }

      result = await host.requestProfile(
        target.route,
        'bot_relay.deliver',
        {
          id: text(envelope?.id),
          body: text(envelope?.body || envelope?.message),
          // Old target gateways ignore the structured fields and consume message.
          message: text(envelope?.message || envelope?.body),
          from_profile: text(envelope?.from_profile),
          from_handle: text(envelope?.from_handle),
          source_install_id: sourceInstallId,
          target_install_id: expectedInstallId,
          courier_namespace_id: namespace,
          profile: text(envelope?.target_profile)
        },
        RELAY_DELIVERY_REQUEST_TIMEOUT_MS
      )
    } catch (error) {
      await nack(sender, envelope, heartbeat, error, isRetryableDeliveryError(error))
      return
    }

    if (heartbeat.lost) {
      // Another courier now owns settlement. Its retry reuses the target-side
      // receipt keyed by event id instead of running the Bot turn twice.
      return
    }

    if (Number(result?.protocol_version) !== 2 || result?.durable_receipt !== true) {
      // An older target may already have run the turn but cannot prove that a
      // retry will replay a durable receipt. Settle as indeterminate instead
      // of automatically executing the same message again.
      await nack(
        sender,
        envelope,
        heartbeat,
        'target gateway did not return a durable relay-v2 receipt; delivery outcome is indeterminate',
        false
      )
      return
    }

    const status = text(result?.status).toLowerCase()
    const reply = text(result?.reply)
    const error = text(result?.error)

    if (status === 'processing' || status === 'in_progress') {
      await nack(
        sender,
        envelope,
        heartbeat,
        'target delivery is still processing behind its durable inbox receipt',
        true,
        result?.retry_after_seconds ?? 30
      )
      return
    }

    if (status === 'indeterminate' || status === 'cancelled') {
      await nack(sender, envelope, heartbeat, error || `target delivery ended in terminal ${status} state`, false)
      return
    }

    if (status === 'failed' || error) {
      await nack(sender, envelope, heartbeat, error || 'target delivery ended in terminal failed state', false)
      return
    }

    if (status !== 'completed' && status !== 'succeeded') {
      await nack(
        sender,
        envelope,
        heartbeat,
        'target gateway returned an unknown durable relay-v2 receipt state',
        false
      )
      return
    }

    try {
      const id = text(envelope?.id)
      const outcomeDigest = await sha256Hex(`${id}\u0000${reply}\u0000${error}`)

      if (heartbeat.lost) {
        return
      }

      await host.requestProfile(sender.route, 'bot_relay.outbox.ack', {
        ...casParams(envelope, heartbeat),
        reply,
        error,
        outcome_digest: outcomeDigest
      })
    } catch (ackError) {
      // ACK may have committed before the transport failed. Never turn an
      // uncertain ACK into NACK; lease recovery + target receipt settle it.
      report(ackError, `ack:${text(envelope?.id)}`)
    }
  }

  async function processV1(sender, envelope, byId) {
    const id = text(envelope?.id)
    const target = byId.get(text(envelope?.target_connection))
    const postReply = async payload => {
      try {
        await host.requestProfile(sender.route, 'bot_relay.reply', { id, ...payload })
      } catch (error) {
        report(error, `v1-reply:${id}`)
      }
    }

    if (!id) {
      return
    }

    if (!target) {
      await postReply({
        error: `connection '${envelope?.target_connection}' is not connected to this Desktop right now`
      })
      return
    }

    try {
      const result = await host.requestProfile(
        target.route,
        'bot_relay.deliver',
        {
          profile: text(envelope?.target_profile),
          message: text(envelope?.message)
        },
        RELAY_DELIVERY_REQUEST_TIMEOUT_MS
      )
      await postReply({ reply: text(result?.reply) })
    } catch (error) {
      await postReply({ error: text(error?.message || error || 'delivery failed') })
    }
  }

  function scheduleEnvelope(sender, envelope, protocol, byId, capabilitiesByTarget, pending) {
    if (protocol === 1) {
      pending.push(executor.run(relayTargetKey(envelope), () => processV1(sender, envelope, byId)))
      return
    }

    const heartbeat = startHeartbeat(sender, envelope)
    const recordKey = [
      namespace,
      sender.id,
      installId(sender.route) || '',
      text(envelope?.id),
      heartbeat.lease.token,
      heartbeat.lease.generation
    ].join('\u0000')
    const record = {
      cancelled: false,
      envelope,
      heartbeat,
      sender,
      state: 'queued'
    }
    leasedTasks.set(recordKey, record)

    const operation = async () => {
      if (record.cancelled) {
        return
      }

      record.state = 'running'
      await processV2(sender, envelope, byId, capabilitiesByTarget, heartbeat)
    }

    pending.push(
      executor.run(relayTargetKey(envelope), operation).finally(() => {
        record.state = 'done'
        heartbeat.stop()
        leasedTasks.delete(recordKey)
      })
    )
  }

  async function releaseUndispatched(sender, envelope, reason) {
    const heartbeat = startHeartbeat(sender, envelope)

    try {
      await nack(sender, envelope, heartbeat, reason, true)
    } finally {
      heartbeat.stop()
    }
  }

  async function drainOnce() {
    if (disposed || drainBusy) {
      return
    }

    drainBusy = true

    try {
      const current = await connections()

      if (current.length < 2) {
        return
      }

      const byId = new Map(current.map(connection => [connection.id, connection]))
      const capabilitiesByTarget = new Map()
      const pending = []
      const v1Processed = new Set()
      let emptyStreak = 0

      // One claim per sender per round. The cursor carries across polls, so a
      // fleet larger than the four global slots cannot let the first source
      // monopolize every lease window.
      while (!disposed && executor.freeSlots > 0 && emptyStreak < current.length) {
        const index = claimCursor % current.length
        const sender = current[index]
        claimCursor = (index + 1) % current.length

        if (v1Processed.has(sender.id)) {
          emptyStreak += 1
          continue
        }

        let batch

        try {
          batch = await claim(sender, 1)
        } catch (error) {
          report(error, `claim:${sender.id}`)
          emptyStreak += 1
          continue
        }

        if (batch.protocol === 1) {
          // v1 drain is destructive and has no limit/lease. Preserve every
          // already-claimed envelope, but never drain this sender twice in one
          // pass.
          v1Processed.add(sender.id)
        }

        if (!batch.envelopes.length) {
          emptyStreak += 1
          continue
        }

        emptyStreak = 0

        if (disposed && batch.protocol === 2) {
          await Promise.allSettled(
            batch.envelopes.map(item => releaseUndispatched(sender, item, 'relay worker stopped before dispatch'))
          )
          break
        }

        const accepted = batch.protocol === 1 ? batch.envelopes : batch.envelopes.slice(0, executor.freeSlots)
        const overflow = batch.protocol === 1 ? [] : batch.envelopes.slice(accepted.length)

        for (const item of accepted) {
          scheduleEnvelope(sender, item, batch.protocol, byId, capabilitiesByTarget, pending)
        }

        if (overflow.length) {
          await Promise.allSettled(
            overflow.map(item => releaseUndispatched(sender, item, 'claim returned more envelopes than requested'))
          )
        }
      }

      await Promise.allSettled(pending)
    } finally {
      drainBusy = false
    }
  }

  function start() {
    disposed = false

    if (!setIntervalFn || !clearIntervalFn) {
      return
    }

    if (rosterTimer === null) {
      rosterTimer = setIntervalFn(() => void syncRosters(), rosterIntervalMs)
      void syncRosters()
    }

    if (drainTimer === null) {
      drainTimer = setIntervalFn(() => void drainOnce(), drainIntervalMs)
    }
  }

  function stop() {
    disposed = true

    if (rosterTimer !== null && clearIntervalFn) {
      clearIntervalFn(rosterTimer)
      rosterTimer = null
    }

    if (drainTimer !== null && clearIntervalFn) {
      clearIntervalFn(drainTimer)
      drainTimer = null
    }

    const releases = []

    for (const [recordKey, record] of leasedTasks) {
      if (record.state !== 'queued' || record.cancelled) {
        // An already-dispatched turn keeps renewing until it can ACK/NACK.
        // Revoking it here would create an avoidable ambiguous-result replay.
        continue
      }

      record.cancelled = true
      releases.push(
        nack(record.sender, record.envelope, record.heartbeat, 'relay worker stopped before dispatch', true).finally(
          () => {
            record.heartbeat.stop()
            leasedTasks.delete(recordKey)
          }
        )
      )
    }

    if (!stopPromise) {
      const budget = Number(stopTimeoutMs)
      const timeoutMs = Number.isFinite(budget) ? Math.max(0, budget) : RELAY_STOP_TIMEOUT_MS
      const quiesce = (async () => {
        await Promise.allSettled(releases)

        // A dispatched target turn keeps its heartbeat and settlement
        // authority. Leadership is not released until those turns, any v1
        // work, and in-flight roster/claim RPCs have all quiesced.
        while (rosterBusy || drainBusy || executor.active || executor.queue.length || leasedTasks.size) {
          await new Promise(resolve => (setTimeoutFn || globalThis.setTimeout)(resolve, 10))
        }

        return { status: 'quiesced' }
      })()

      if (!setTimeoutFn) {
        stopPromise = quiesce
      } else {
        let timeoutId = null
        const timeout = new Promise(resolve => {
          timeoutId = setTimeoutFn(
            () => resolve({ status: 'timed_out_with_live_authority' }),
            timeoutMs
          )
        })
        stopPromise = Promise.race([quiesce, timeout]).finally(() => {
          if (timeoutId !== null && clearTimeoutFn) {
            clearTimeoutFn(timeoutId)
          }
        })
      }
    }

    return stopPromise
  }

  return {
    courierId,
    courierNamespaceId: namespace,
    drainOnce,
    start,
    stop,
    syncRosters
  }
}

/** Renderer lifecycle around the process-wide Electron leadership grant.
 * Every full window loads plugins, so only the main-process grant may create a
 * worker. The grant stays held until worker.stop() has drained all live work. */
export function createBotRelaySupervisor({
  host,
  leadership = host?.botRelayLeadership,
  resolveHandle,
  createWorker = createBotRelayWorker,
  createRuntimeId = createRelayRuntimeId,
  onLeadershipChange = () => undefined,
  onError = () => undefined,
  setTimeoutFn = globalThis.setTimeout?.bind(globalThis),
  clearTimeoutFn = globalThis.clearTimeout?.bind(globalThis),
  leadershipRetryMs = RELAY_LEADERSHIP_RETRY_MS
}) {
  if (typeof leadership?.acquire !== 'function' || typeof leadership?.release !== 'function') {
    throw new TypeError('bot relay requires Desktop leadership authority')
  }

  let disposed = false
  let started = false
  let retryTimer = null
  let attemptPromise = null
  let worker = null
  let leadershipToken = ''
  let stopPromise = null

  const report = (error, context) => {
    try {
      onError(error, context)
    } catch {
      // Diagnostics never become leadership authority.
    }
  }

  const publishLeadership = namespace => {
    try {
      onLeadershipChange(namespace)
    } catch (error) {
      report(error, 'leadership-change')
    }
  }

  const release = async token => {
    if (!token) {
      return
    }

    try {
      await leadership.release(token)
    } catch (error) {
      report(error, 'leadership-release')
    }
  }

  const scheduleRetry = rawDelay => {
    if (disposed || retryTimer !== null || !setTimeoutFn) {
      return
    }

    const delay = Number(rawDelay)
    const retryAfter = Number.isFinite(delay) ? Math.max(10, Math.min(delay, 60_000)) : leadershipRetryMs

    retryTimer = setTimeoutFn(() => {
      retryTimer = null
      void attempt()
    }, retryAfter)
  }

  const runAttempt = async () => {
    if (disposed || worker) {
      return
    }

    let grant

    try {
      grant = await leadership.acquire()
    } catch (error) {
      report(error, 'leadership-acquire')
      scheduleRetry(leadershipRetryMs)
      return
    }

    if (!grant?.acquired) {
      scheduleRetry(grant?.retryAfterMs ?? leadershipRetryMs)
      return
    }

    const token = text(grant?.leadershipToken)
    const namespace = validCourierNamespace(grant?.courierNamespaceId)

    if (!token || !namespace) {
      await release(token)
      report(new Error('Desktop returned an invalid bot relay leadership grant'), 'leadership-grant')
      scheduleRetry(leadershipRetryMs)
      return
    }

    if (disposed) {
      await release(token)
      return
    }

    leadershipToken = token

    try {
      worker = createWorker({
        host,
        courierNamespaceId: namespace,
        courierId: createRuntimeId('courier'),
        resolveHandle,
        onError
      })
      publishLeadership(namespace)
      worker.start()
    } catch (error) {
      const failedWorker = worker
      worker = null
      leadershipToken = ''
      publishLeadership(null)

      try {
        await failedWorker?.stop?.()
      } catch (stopError) {
        report(stopError, 'worker-stop-after-start-failure')
      }

      await release(token)
      report(error, 'worker-start')
      scheduleRetry(leadershipRetryMs)
    }
  }

  function attempt() {
    if (!attemptPromise) {
      attemptPromise = runAttempt().finally(() => {
        attemptPromise = null
      })
    }

    return attemptPromise
  }

  function start() {
    if (started || disposed) {
      return
    }

    started = true
    void attempt()
  }

  function stop() {
    disposed = true

    if (retryTimer !== null && clearTimeoutFn) {
      clearTimeoutFn(retryTimer)
      retryTimer = null
    }

    if (!stopPromise) {
      stopPromise = (async () => {
        if (attemptPromise) {
          await attemptPromise
        }

        const activeWorker = worker
        const token = leadershipToken

        try {
          const stopResult = await activeWorker?.stop?.()
          if (stopResult?.status === 'timed_out_with_live_authority') {
            report(new Error('bot relay worker stop timed out with live authority'), 'worker-stop-timeout')
            return
          }
        } catch (error) {
          report(error, 'worker-stop')
          return
        }

        worker = null
        leadershipToken = ''
        publishLeadership(null)
        await release(token)
      })()
    }

    return stopPromise
  }

  return { start, stop }
}
