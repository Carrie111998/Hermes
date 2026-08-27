import { readFile } from 'node:fs/promises'
import { join } from 'node:path'

/**
 * Detect whether a Hermes gateway is already running locally (e.g. installed
 * via `hermes gateway install` as a Scheduled Task / systemd / launchd) so the
 * Desktop app can adopt it instead of spawning a duplicate `hermes serve`.
 *
 * Uses the same liveness contract as `gateway/status.py`:
 * - `gateway_state.json` must have `gateway_state: "running"`
 * - `pid` must be a live OS process (PID-reuse guarded by `start_time`)
 *
 * Liveness keys off the PID alone. Staleness of `updated_at` is only a signal
 * *together with a dead PID*: `hermes_cli/gateway.py` only flags a record when
 * it is stale AND its recorded process is gone ("the file is contradicting
 * reality" — likely an ungraceful shutdown). A live PID whose start_time
 * fingerprint still matches wins regardless of `updated_at` age; staleness can
 * never overrule a live process. Gating adoption on freshness alone therefore
 * wrongly rejected running gateways (bug #91564: Desktop spawned a duplicate
 * serve next to a still-live `gateway run`).
 *
 * `updated_at` is kept for that one classification: it can only ever *explain*
 * a dead PID (stale => record outlived an ungraceful kill), never overrule a
 * live one.
 *
 * Pure detection — no side effects, no spawn, no network. Safe to call on every
 * boot.
 */

const RUNTIME_STATUS_FILENAME = 'gateway_state.json'
/** Matches `gateway/status.py:_RUNTIME_STATUS_STALE_TTL_S` */
const STALE_TTL_MS = 120_000

export type GatewayLivenessReason =
  | 'state-running'
  | 'state-not-running'
  | 'state-missing'
  | 'state-stale'
  | 'pid-missing'
  | 'pid-dead'
  | 'pid-reused'
  | 'no-state'
  | 'no-hermes-home'

export type GatewayLiveness = {
  /** True only when a local gateway is confirmed alive and should be adopted. */
  alive: boolean
  /** Live PID, when determinable (adoption uses this for ownership bookkeeping). */
  pid: number | null
  /** Listening port of the live gateway, when discoverable. */
  port: number | null
  /** Why the gateway is considered alive or not (for diagnostics/logging). */
  reason: GatewayLivenessReason
}

type GatewayStateRecord = {
  gateway_state?: unknown
  pid?: unknown
  start_time?: unknown
  updated_at?: unknown
  argv?: unknown
}

function hermesHome(): string | null {
  const home = process.env.HERMES_HOME
  if (home && home.trim()) return home.trim()
  const fallback = process.env.HOME || process.env.USERPROFILE
  if (fallback && fallback.trim()) return fallback.trim()
  return null
}

async function readJsonSafe<T extends Record<string, unknown>>(filePath: string): Promise<T | null> {
  try {
    const raw = await readFile(filePath, 'utf8')
    const parsed = JSON.parse(raw)
    return typeof parsed === 'object' && parsed !== null ? (parsed as T) : null
  } catch {
    return null
  }
}

/**
 * Live start-time fingerprint for `pid`, or `null` when it can't be read.
 *
 * Mirrors `gateway/status.py:_get_process_start_time`'s FIRST source only:
 * field 22 of `/proc/<pid>/stat` (start time in clock ticks). Python's second
 * source is `psutil.Process().create_time()`, which has no dependency-free
 * Node equivalent, so on platforms without `/proc` (Windows, macOS) this
 * returns `null` and the reuse guard abstains rather than guessing.
 *
 * Abstaining is the same contract Python applies: the fingerprints are only
 * ever compared when BOTH the recorded and the live value are known, so a
 * `null` here can never turn a live gateway into a false `pid-reused`.
 *
 * Note on parsing: Python's own reader (`gateway/status.py`) uses a naive
 * `split()[21]`; this module splits after the final ')' so comms containing
 * spaces are still parsed correctly. The two diverge only for such comms,
 * which a Python gateway's comm (a kernel-truncated basename) never is.
 */
async function liveStartTime(pid: number): Promise<number | null> {
  try {
    const raw = await readFile(`/proc/${pid}/stat`, 'utf8')
    // Field 22 (1-indexed) is starttime. comm (field 2) is parenthesized and
    // may contain spaces, so split after the final ')' instead of naively.
    const tail = raw.slice(raw.lastIndexOf(')') + 1).trim().split(/\s+/)
    // After comm, the remaining fields start at 3, so starttime is index 19.
    const value = Number(tail[19])
    return Number.isFinite(value) ? value : null
  } catch {
    return null
  }
}

/**
 * Whether `pid` is a live process that is still the one the record described.
 *
 * `reused` is only ever returned on a POSITIVE mismatch (both fingerprints
 * known and different) — an unreadable fingerprint abstains.
 */
async function isPidAlive(
  pid: number,
  recordedStart: number | null
): Promise<{ alive: boolean; reason: 'alive' | 'dead' | 'reused' }> {
  try {
    // Signal 0 is a no-op probe — throws ESRCH if the PID doesn't exist.
    process.kill(pid, 0)
  } catch {
    // ESRCH = gone. EPERM = exists but owned by another user: it is alive,
    // yet it cannot be our gateway, so it is unusable for adoption. (Python's
    // `_pid_exists` reports EPERM as alive for the status view; here we
    // classify adoption eligibility instead, so both land on 'dead'.)
    return { alive: false, reason: 'dead' }
  }
  if (recordedStart !== null) {
    const current = await liveStartTime(pid)
    if (current !== null && current !== recordedStart) {
      return { alive: false, reason: 'reused' }
    }
  }
  return { alive: true, reason: 'alive' }
}

/** Recorded `start_time` as a number, or `null` when absent/unusable. */
function recordedStartTime(record: GatewayStateRecord): number | null {
  const raw = record.start_time
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw
  if (typeof raw === 'string' && raw.trim()) {
    const parsed = Number(raw)
    if (Number.isFinite(parsed)) return parsed
  }
  return null
}

function isoTsToMs(iso: string): number {
  try {
    return Date.parse(iso)
  } catch {
    return NaN
  }
}

/**
 * Best-effort detection of a live local gateway. Never throws — any error
 * degrades to `{ alive: false }` so callers can fall back to spawning.
 */
export async function detectLocalGatewayRunning(): Promise<GatewayLiveness> {
  const home = hermesHome()
  if (!home) {
    return { alive: false, pid: null, port: null, reason: 'no-hermes-home' }
  }

  const statePath = join(home, RUNTIME_STATUS_FILENAME)
  const record = await readJsonSafe<GatewayStateRecord>(statePath)
  if (!record) {
    return { alive: false, pid: null, port: null, reason: 'no-state' }
  }

  if (record.gateway_state !== 'running') {
    return { alive: false, pid: null, port: null, reason: 'state-not-running' }
  }

  const pidRaw = record.pid
  if (typeof pidRaw !== 'number' || !Number.isFinite(pidRaw) || pidRaw <= 0) {
    return { alive: false, pid: null, port: null, reason: 'pid-missing' }
  }
  const pid = pidRaw

  // PID liveness decides adoption. `updated_at` is NOT consulted here: an idle
  // gateway legitimately keeps a timestamp older than the TTL, so gating on it
  // rejected exactly the healthy gateways this module is meant to adopt.
  const live = await isPidAlive(pid, recordedStartTime(record))
  if (!live.alive) {
    if (live.reason === 'reused') {
      return { alive: false, pid, port: null, reason: 'pid-reused' }
    }
    // The PID is gone. A record ALSO past its freshness TTL is the signature of
    // an ungraceful kill whose shutdown handler never ran (same reading as
    // `hermes_cli/gateway.py`'s stale-state warning), which is worth
    // distinguishing in logs from a clean, freshly-recorded exit.
    const updatedAtMs = isoTsToMs(String(record.updated_at || ''))
    const stale = Number.isNaN(updatedAtMs) || Date.now() - updatedAtMs > STALE_TTL_MS
    return { alive: false, pid, port: null, reason: stale ? 'state-stale' : 'pid-dead' }
  }

  const port = extractGatewayPort(record)

  return { alive: true, pid, port, reason: 'state-running' }
}

/**
 * Extract the listening port from the gateway's runtime state. Order of
 * precedence:
 * 1. `--port N` on the recorded argv
 * 2. `gateway_state.json → port` field (newer gateways may emit this)
 * 3. `null` (caller must fall back to probing /api/status or default)
 */
function extractGatewayPort(record: GatewayStateRecord): number | null {
  const argv = record.argv
  if (Array.isArray(argv)) {
    for (let i = 0; i < argv.length; i++) {
      const arg = String(argv[i])
      if ((arg === '--port' || arg === '-p') && i + 1 < argv.length) {
        const n = Number(argv[i + 1])
        if (Number.isFinite(n) && n > 0 && n < 65536) return n
      }
      // Also accept `--port=N` form.
      if (arg.startsWith('--port=')) {
        const n = Number(arg.slice('--port='.length))
        if (Number.isFinite(n) && n > 0 && n < 65536) return n
      }
    }
  }
  return null
}

/** Cached TTL for the liveness probe so the 5s roster poll doesn't hammer disk. */
const DETECT_CACHE_TTL_MS = 5_000
let cachedResult: GatewayLiveness | null = null
let cachedAt = 0

/**
 * Cached wrapper for `detectLocalGatewayRunning()` — callers that poll
 * frequently (roster enumeration, boot retries) share one probe per TTL.
 */
export async function detectLocalGatewayRunningCached(): Promise<GatewayLiveness> {
  const now = Date.now()
  if (cachedResult !== null && now - cachedAt < DETECT_CACHE_TTL_MS) {
    return cachedResult
  }
  const result = await detectLocalGatewayRunning()
  cachedResult = result
  cachedAt = now
  return result
}

/** Invalidate the cache — call when gateway state is known to have changed. */
export function invalidateLocalGatewayCache(): void {
  cachedResult = null
  cachedAt = 0
}
