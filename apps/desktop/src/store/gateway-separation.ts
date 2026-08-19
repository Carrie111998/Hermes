/**
 * True separation by gateway.
 *
 * Upstream already tags cross-gateway session rows with `connection_id` (the
 * #88880 unified-list splice) and computes a union agent roster
 * (`getAgentRoster`), but neither the Sessions sidebar nor the profile rail
 * renders the gateway dimension: two machines that both expose a `default`
 * profile collapse into one indistinguishable bucket.
 *
 * This module carries the renderer-side state the sidebar needs to tell them
 * apart. It deliberately does NOT touch `$profiles` — that cache feeds
 * delegation, @-mentions, filters and the SDK, and injecting foreign agents
 * into it would leak cross-machine profiles into all of them. The rail renders
 * these agents as EXTRA squares instead.
 */
import { atom, computed } from 'nanostores'

import { $gateway, activeGatewayConnectionId } from './gateway'
import { selectProfile as selectProfileImpl } from './profile'

/** Fallback id for the app-managed local runtime. */
export const LOCAL_CONNECTION_ID = 'local'

/** One agent in the union roster: a profile living on a registered source. */
export interface RosterAgent {
  connectionId: string
  connectionLabel: string
  profile: string
  handle: string
  reachable: boolean
}

/** connectionId → human device name ("Mecha Hermes (HP)"). */
export const $connectionLabels = atom<Record<string, string>>({})

/** The registry primary. Session rows WITHOUT a `connection_id` came from it. */
export const $primaryConnectionId = atom<string>('')

/** Registry id the live gateway is serving, or '' for the primary's own route.
 *
 *  Mirrored from the registry rather than written optimistically by the rail.
 *  The optimistic version raced: clicking a square set the attachment in the
 *  same frame as the click, the sidebar's reseed fired on that change, and the
 *  fetch went out while the API layer's ambient connection was still the
 *  PREVIOUS machine's — so the trip home from the Dell re-fetched the Dell's
 *  rows and then relabelled them as the primary's. `applyActive` publishes
 *  `$gateway` in the same synchronous frame it sets the ambient connection, so
 *  reacting to that is race-free by construction. */
const $liveRegistryConnection = atom<string>('')

$gateway.subscribe(() => {
  $liveRegistryConnection.set(activeGatewayConnectionId() || '')
})

/** The connection the live gateway is currently attached to. Resolved against
 *  the registry primary, so it stays correct as the registry finishes loading
 *  (the raw mirror above is '' for the primary's own route). */
export const $attachedConnectionId = computed(
  [$liveRegistryConnection, $primaryConnectionId],
  (live, primary) => live || primary || LOCAL_CONNECTION_ID
)

/** Every (connection, profile) pair across the registry. */
export const $rosterAgents = atom<RosterAgent[]>([])

/** True once more than one connection is registered. Everything this module
 *  adds stays invisible below that line, so single-gateway users see the
 *  stock sidebar. */
export const $multiGateway = computed($connectionLabels, labels => Object.keys(labels).length > 1)

/** Stable identity for a rail square / session group: a profile name alone is
 *  ambiguous once two machines both serve `default`. */
export const agentKey = (connectionId: string, profile: string): string =>
  `${(connectionId || LOCAL_CONNECTION_ID).trim()}::${(profile || 'default').trim().toLowerCase()}`

/** Which connection a session row belongs to.
 *
 *  A row spliced from another gateway carries an explicit `connection_id` and is
 *  self-describing. An UNTAGGED row belongs to whichever gateway served the
 *  list — and the `/api/sessions` family now carries the active connection, so
 *  that is the ATTACHED one, not always the primary. Attributing untagged rows
 *  to the primary instead is what made the Dell's own sessions vanish: they came
 *  back untagged from the Dell, were labelled as the HP's, and the sidebar's
 *  connection filter then hid every one of them behind "No sessions yet". */
export function sessionConnectionId(session: { connection_id?: string }): string {
  const tagged = (session.connection_id || '').trim()

  return tagged || $attachedConnectionId.get() || $primaryConnectionId.get() || LOCAL_CONNECTION_ID
}

/** Build "is this row on the gateway the rail is currently attached to?".
 *
 *  The sidebar's scope filter used to ask only "is this row's PROFILE the active
 *  one", and a profile name is not an identity across machines: while attached
 *  to the Dell's `default` that predicate still matched every one of the HP's
 *  `default` rows, so switching gateway changed nothing on screen even though
 *  the socket had genuinely moved. Adding the connection dimension is what makes
 *  the hop visible.
 *
 *  A factory rather than a bare predicate so React sees the inputs: the caller
 *  memoizes on (multiGateway, attached) and the scoping recomputes when the live
 *  gateway hops. Inert below two connections, so single-gateway installs keep
 *  the stock filter exactly. */
export function onAttachedConnection(
  multiGateway: boolean,
  attached: string
): (session: { connection_id?: string }) => boolean {
  if (!multiGateway || !attached) {
    return () => true
  }

  return session => sessionConnectionId(session) === attached
}

/** Device name for a connection id, or '' when unknown / single-gateway. */
export function gatewayLabel(connectionId: string): string {
  return $connectionLabels.get()[connectionId] || ''
}

/** Agents that are NOT on the connection the live gateway is attached to.
 *
 *  Keyed on the ATTACHED connection because that is what `$profiles` describes.
 *  `refreshProfiles` goes through `hermesApi`, which carries the ambient
 *  connection, and `applyActive` republishes that ambient connection on every
 *  switch — so the rail's own squares follow the live gateway (the same
 *  invariant #85731 relies on: "the list the new backend just served").
 *
 *  Native squares = the attached machine's profiles, foreign squares =
 *  everything else. Complementary by construction, and re-derived on every hop
 *  rather than pinned to one machine — keying these on the PRIMARY instead
 *  rendered the attached box's agents twice the moment you hopped, once as
 *  native squares and once as foreign ones. */
export const $foreignAgents = computed([$rosterAgents, $attachedConnectionId], (agents, attached) =>
  agents.filter(agent => agent.connectionId !== (attached || LOCAL_CONNECTION_ID).trim())
)

/** Refresh labels, primary and the union roster. Attachment is derived from
 *  the live gateway (see `$attachedConnectionId`), not fetched here. Every leg is
 *  best-effort and independent: an older build without the registry or the
 *  roster simply leaves those stores empty, which switches the whole feature
 *  off rather than breaking the sidebar. */
export async function refreshGatewaySeparation(): Promise<void> {
  const desktop = window.hermesDesktop as unknown as Record<string, any>
  let connections: { id: string; kind: string; url?: string }[] = []

  try {
    const registry = await desktop?.connections?.list?.()

    if (registry) {
      connections = registry.connections ?? []

      const labels: Record<string, string> = {}

      for (const connection of connections) {
        labels[connection.id] = (connection as { label?: string }).label || connection.id
      }

      $connectionLabels.set(labels)
      $primaryConnectionId.set((registry.primary || '').trim() || LOCAL_CONNECTION_ID)
    }
  } catch {
    // Registry unavailable — leave the feature off.
  }

  try {
    const roster = await desktop?.getAgentRoster?.()

    if (roster) {
      const unreachable = new Set(
        (roster.sources ?? [])
          .filter((s: { reachable?: boolean }) => !s.reachable)
          .map((s: { connectionId: string }) => s.connectionId)
      )

      $rosterAgents.set(
        (roster.agents ?? []).map((agent: Record<string, string>) => ({
          connectionId: agent.connectionId,
          connectionLabel: agent.connectionLabel || agent.connectionId,
          profile: agent.profile || 'default',
          handle: agent.handle || agent.profile || 'default',
          reachable: !unreachable.has(agent.connectionId)
        }))
      )
    }
  } catch {
    // Roster unavailable — the rail keeps its single-source squares.
  }
}

/** Switch the live gateway to an agent on another machine. Routes through the
 *  connection-scoped activation path (`ensureGatewayAgent`), not a same-named
 *  local profile. Imported lazily to keep this module out of the profile
 *  store's import cycle. */
/** Activate a specific (connection, profile) agent and move the chat scope onto
 *  it — the cross-machine analogue of upstream's `selectProfile`.
 *
 *  Everything here addresses the agent by its OWNING TUPLE. A bare profile name
 *  resolves through the legacy profile-only pool, which is anchored to the
 *  primary/local runtime, so a named profile on a secondary would activate the
 *  same-named profile on the WRONG machine (#88880 / #89466). */
async function activateAgent(connectionId: string, profile: string, collapseAllProfiles: boolean): Promise<void> {
  const { $newChatProfile, ensureGatewayAgent, requestFreshSession, setShowAllProfiles } = await import('./profile')

  if (collapseAllProfiles) {
    setShowAllProfiles(false)
  }

  await ensureGatewayAgent(connectionId, profile)

  // Point new chats at THIS agent's profile. `desktopSessionCreateParams` reads
  // `$newChatProfile` and calls `ensureGatewayProfile` on it before creating the
  // session; left pointing at a profile this machine does not serve, that call
  // swaps the live gateway away from here and the message lands on the primary.
  $newChatProfile.set(profile)

  // Attachment is NOT written here: it is derived from the live gateway that
  // `ensureGatewayAgent` just published, so a rejected activation cannot leave
  // the rail claiming a machine we never reached.

  // Land on a fresh draft, exactly like `selectProfile` does for a same-machine
  // switch. Upstream reaches that through a profile-NAME change, which a hop
  // between two machines' `default` never produces — so without this you kept
  // staring at the previous machine's open chat while the composer had already
  // moved to the new one.
  requestFreshSession()
}

/** Pick an agent that lives on a DIFFERENT gateway than the live one. */
export async function selectForeignAgent(agent: RosterAgent): Promise<void> {
  await activateAgent(agent.connectionId, agent.profile, false)
}

/** Per-backend caches are invalidated on a change of profile NAME, which a hop
 *  between two machines' `default` never produces. Mirror that trigger onto the
 *  connection so a gateway hop drops the previous machine's settings, models and
 *  session list exactly like a profile switch does. Skips the initial seed. */
let lastRoutedConnection: null | string = null

$attachedConnectionId.subscribe(value => {
  const next = (value || '').trim()

  if (lastRoutedConnection !== null && lastRoutedConnection !== next) {
    // Both are best-effort side effects of a switch that has ALREADY landed, so
    // neither may reject: an unhandled rejection here would surface as a crash
    // report for a gateway hop that actually succeeded.
    void import('./profile')
      .then(({ invalidateProfileRoutedCaches }) => invalidateProfileRoutedCaches())
      .catch(() => undefined)
    void reseedWorkspaceForConnection()
  }

  lastRoutedConnection = next
})

/** Re-point the workspace at the machine we just landed on.
 *
 *  A cwd is a path on ONE box. Carrying the previous gateway's across a hop
 *  left the file tree asking the new machine for a directory it does not have,
 *  and the panel rendered "Could not read this folder (ENOENT)" — or, worse,
 *  silently listed a same-named path. `/api/fs/default-cwd` is connection-scoped
 *  now, so asking again answers for the machine we are actually on.
 *
 *  Skipped while a session is open: that session owns the cwd, and a hop starts
 *  a fresh draft anyway. Best-effort — a failure leaves the previous path rather
 *  than blanking the panel. */
async function reseedWorkspaceForConnection(): Promise<void> {
  try {
    const [{ $activeSessionId, setCurrentBranch, setCurrentCwdTransient }, { desktopDefaultCwd }] = await Promise.all([
      import('./session'),
      import('@/lib/desktop-fs')
    ])

    if ($activeSessionId.get()) {
      return
    }

    const next = await desktopDefaultCwd()

    // Re-check: the user may have opened a session while this was in flight.
    if (!next?.cwd || $activeSessionId.get()) {
      return
    }

    setCurrentCwdTransient(next.cwd)
    setCurrentBranch(next.branch || '')
  } catch {
    // Never rejects — see the call site. Keeping the previous path is the right
    // failure mode: the panel shows a stale tree rather than nothing at all.
  }
}

/** Pick one of the rail's own squares.
 *
 *  Those squares are drawn from `$profiles`, which describes whichever machine
 *  the live gateway is ATTACHED to — `refreshProfiles` goes through `hermesApi`,
 *  which carries the ambient connection. So "the rail's own agent" is only the
 *  primary's while we are actually on the primary.
 *
 *  That makes the owning connection load-bearing rather than decorative. Handing
 *  the bare name to `selectProfile` sends it through `ensureGatewayProfile`, the
 *  profile-only pool anchored to the primary/local runtime: while attached to a
 *  secondary, clicking its `appdev` square would activate the PRIMARY's `appdev`
 *  and point the next chat at the wrong machine. Only a null owner — the
 *  primary's own route, and every single-gateway install — may take the legacy
 *  path, where it stays byte-identical to upstream. */
export function selectPrimaryAgent(profile: string): void {
  const owner = (activeGatewayConnectionId() ?? '').trim()

  if (!owner) {
    selectProfileImpl(profile)

    return
  }

  void activateAgent(owner, profile, true)
}
