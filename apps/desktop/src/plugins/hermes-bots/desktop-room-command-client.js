const MAX_COMMANDS_PER_CLAIM = 8
const MAX_COMMANDS_PER_WAKE = 64
const MAX_ROOM_IDS_PER_CLAIM = 128
const LEASE_RENEW_INTERVAL_MS = 15_000

/** Stable identity shared by the bounded gateway projection and command queue. */
export function desktopRoomIdentity(name, room) {
  const roomId = String(room?.roomId || '').trim()
  return roomId || `name:${String(name || '').trim()}`
}

/** Classic rooms this Desktop can coordinate. Hosted rooms run on a gateway. */
export function desktopRoomDescriptors(rooms) {
  return Object.entries(rooms || {})
    .filter(([, room]) => {
      const hosted = typeof room?.hosted === 'string' ? room.hosted.trim() : ''
      return !hosted && !room?.tombstone && Array.isArray(room?.log)
    })
    .map(([name, room]) => ({
      name,
      roomId: desktopRoomIdentity(name, room)
    }))
    .filter(room => room.roomId && room.name)
}

export function createDesktopRoomConsumerId() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return `desktop:${globalThis.crypto.randomUUID()}`
  }
  return `desktop:${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function boundedError(error) {
  const text = String(error?.message || 'Desktop could not apply the room command')
    .replace(/\s+/g, ' ')
    .trim()
  return text.slice(0, 240)
}

/**
 * Claim and apply classic-room commands from every reachable default gateway.
 * Missing methods identify an older backend and simply disable this fallback.
 */
export async function runDesktopRoomCommandCycle({
  routes,
  consumerId,
  rooms,
  request,
  execute,
  shouldContinue = () => true
}) {
  const descriptors = desktopRoomDescriptors(rooms)
  if (!descriptors.length) return []

  const roomBatches = []
  for (let index = 0; index < descriptors.length; index += MAX_ROOM_IDS_PER_CLAIM) {
    roomBatches.push(descriptors.slice(index, index + MAX_ROOM_IDS_PER_CLAIM))
  }
  const seenConnections = new Set()
  const outcomes = []

  for (const route of Array.isArray(routes) ? routes : []) {
    const connectionId = String(route?.connectionId || '')
    const routeKey = connectionId || '__active__'
    if (seenConnections.has(routeKey)) continue
    seenConnections.add(routeKey)

    let remaining = MAX_COMMANDS_PER_WAKE
    for (const batch of roomBatches) {
      if (remaining <= 0) return outcomes

      while (remaining > 0) {
        const claimLimit = Math.min(MAX_COMMANDS_PER_CLAIM, remaining)
        let claimed
        try {
          claimed = await request(route, 'groups.desktop.claim', {
            consumer_id: consumerId,
            room_ids: batch.map(room => room.roomId),
            limit: claimLimit
          })
        } catch {
          break
        }

        const commands = Array.isArray(claimed?.commands) ? claimed.commands : []
        remaining -= commands.length
        for (const command of commands) {
          if (!shouldContinue()) return outcomes
          let success = false
          let result
          let renewTimer = null
          const leaseToken = String(command?.lease_token || '')
          if (leaseToken && typeof setInterval === 'function') {
            renewTimer = setInterval(() => {
              if (!shouldContinue()) return
              void request(route, 'groups.desktop.renew', {
                consumer_id: consumerId,
                command_id: command.command_id,
                lease_token: leaseToken
              }).catch(() => undefined)
            }, LEASE_RENEW_INTERVAL_MS)
          }
          try {
            result = await execute(command, descriptors)
            success = true
          } catch (error) {
            if (error?.retryable === true) {
              // Keep the lease unacknowledged. It expires back to pending so a
              // temporary member outage never turns into a terminal failure.
              outcomes.push({
                commandId: command.command_id,
                connectionId: routeKey,
                success: false,
                retryable: true
              })
              continue
            }
            result = { message: boundedError(error) }
          } finally {
            if (renewTimer !== null && typeof clearInterval === 'function') {
              clearInterval(renewTimer)
            }
          }

          try {
            await request(route, 'groups.desktop.complete', {
              consumer_id: consumerId,
              command_id: command.command_id,
              lease_token: leaseToken,
              success,
              result
            })
          } catch {
            // The lease expires and a later cycle retries. Send effects use the
            // command id as their room-log id, so a lost ACK cannot duplicate text.
          }
          outcomes.push({ commandId: command.command_id, connectionId: routeKey, success })
        }

        if (commands.length < claimLimit) break
      }
    }
  }

  return outcomes
}
