import { registryBackendScopeKey } from '@hermes/shared'
import { atom, computed } from 'nanostores'

import { readKey, writeKey } from '@/lib/storage'
import { $activeGatewayIdentity, type ActiveGatewayIdentity } from '@/store/gateway'
import { normalizeProfileKey } from '@/store/profile'
import { $currentCwd } from '@/store/session'

import { setTerminalTakeover } from '../store'

import { seedAgentTerminalCommand } from './agent-terminal-stream'

/** One in-app terminal tab. `id` is the renderer-side handle (distinct from the
 *  PTY session id the main process mints); each instance owns its own shell. */
export interface TerminalEntry {
  id: string
  /** Display label. `auto` adopts the resolved shell name until the user renames. */
  title: string
  auto: boolean
  /** Working directory, snapshotted once at creation. Terminals live outside
   *  session/project state — they inherit this initial cwd and the gateway
   *  profile that owns the shell. Switching sessions never moves or recreates
   *  a terminal; at most it re-SELECTS a same-profile tab already pointed at the
   *  session's cwd (see the $currentCwd listener). */
  cwd: string
  /** Registry connection whose backend owns this shell. Absent/null means the
   *  local/legacy v1 route. */
  connectionId?: string
  /** Gateway profile whose backend owns this shell. Snapshotted with the cwd
   *  and connection id so context switches never move a terminal between
   *  hosts. Absent only on tabs persisted by older Desktop versions. */
  profile?: string
  /** Last observed working directory of the live shell (tracked via the PTY
   *  cwd probe / OSC 7). Used to reopen the tab where the user last `cd`'d
   *  rather than the original launch dir. User tabs only. */
  restoreCwd?: string
  /** Serialized xterm scrollback from the last session, replayed on relaunch so
   *  the tab reopens with its recent history (VS Code parity). Processes are NOT
   *  revived — a fresh shell starts beneath the restored buffer. Captured live
   *  for user tabs only; agent mirrors stay runtime-only. */
  reviveBuffer?: string
  /** `user` = interactive PTY shell. `agent` = read-only mirror of an agent
   *  background process (`terminal(background=true)`), keyed by `procId`. */
  kind: 'user' | 'agent'
  procId?: string
}

interface PersistedTerminalEntry {
  auto: boolean
  connectionId?: string
  cwd: string
  id: string
  profile?: string
  restoreCwd?: string
  reviveBuffer?: string
  title: string
}

interface PersistedTerminalState {
  activeTerminalId: null | string
  terminals: PersistedTerminalEntry[]
}

const TERMINALS_STORAGE_KEY = 'hermes.desktop.terminals.v1'

// Cap a single tab's replayed history so the persisted layout can't blow the
// localStorage quota. Roughly mirrors VS Code's persistentSessionScrollback
// default (100 lines) once the serialized escape codes are counted in.
const MAX_REVIVE_BUFFER_CHARS = 48_000

function sanitizePersistedTerminal(value: unknown): PersistedTerminalEntry | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const record = value as Record<string, unknown>
  const id = typeof record.id === 'string' ? record.id.trim() : ''
  const title = typeof record.title === 'string' ? record.title.trim() : ''
  const cwd = typeof record.cwd === 'string' ? record.cwd : ''
  const restoreCwd = typeof record.restoreCwd === 'string' && record.restoreCwd ? record.restoreCwd : undefined
  const reviveBuffer = typeof record.reviveBuffer === 'string' ? record.reviveBuffer : undefined
  const connectionId = typeof record.connectionId === 'string' && record.connectionId.trim() ? record.connectionId.trim() : undefined

  const profile =
    typeof record.profile === 'string' && /^[a-z0-9][a-z0-9_-]{0,63}$/.test(record.profile.trim())
      ? record.profile.trim()
      : undefined

  if (!id) {
    return null
  }

  return {
    auto: typeof record.auto === 'boolean' ? record.auto : true,
    ...(connectionId ? { connectionId } : {}),
    cwd,
    id,
    ...(profile ? { profile } : {}),
    ...(restoreCwd ? { restoreCwd } : {}),
    ...(reviveBuffer ? { reviveBuffer } : {}),
    title: title || 'Terminal'
  }
}

function loadPersistedTerminals(): PersistedTerminalState {
  const fallback: PersistedTerminalState = { activeTerminalId: null, terminals: [] }
  const raw = readKey(TERMINALS_STORAGE_KEY)

  if (!raw) {
    return fallback
  }

  try {
    const parsed = JSON.parse(raw) as unknown

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return fallback
    }

    const record = parsed as Record<string, unknown>

    const terminals = Array.isArray(record.terminals)
      ? record.terminals.map(sanitizePersistedTerminal).filter((term): term is PersistedTerminalEntry => Boolean(term))
      : []

    const active =
      typeof record.activeTerminalId === 'string' && terminals.some(term => term.id === record.activeTerminalId)
        ? record.activeTerminalId
        : (terminals[0]?.id ?? null)

    return { activeTerminalId: active, terminals }
  } catch {
    return fallback
  }
}

// Persist synchronously on every change (the app-wide convention — see panes.ts
// / layout.ts). Capturing history this way means a snapshot is already on disk
// well before the renderer tears down, so app quit needs no unload hook.
function persistTerminals(list: readonly TerminalEntry[], activeTerminalId: null | string) {
  const terminals = list
    .filter(term => term.kind === 'user')
    .map(term => ({
      auto: term.auto,
      ...(term.connectionId ? { connectionId: term.connectionId } : {}),
      cwd: term.cwd,
      id: term.id,
      ...(term.profile ? { profile: term.profile } : {}),
      ...(term.restoreCwd ? { restoreCwd: term.restoreCwd } : {}),
      ...(term.reviveBuffer ? { reviveBuffer: term.reviveBuffer } : {}),
      title: term.title
    }))

  if (!terminals.length) {
    writeKey(TERMINALS_STORAGE_KEY, null)

    return
  }

  const active = terminals.some(term => term.id === activeTerminalId) ? activeTerminalId : (terminals[0]?.id ?? null)
  writeKey(TERMINALS_STORAGE_KEY, JSON.stringify({ activeTerminalId: active, terminals }))
}

const restored = loadPersistedTerminals()

export const $terminals = atom<readonly TerminalEntry[]>(
  restored.terminals.map(term => ({ ...term, kind: 'user' as const }))
)
export const $activeTerminalId = atom<string | null>(restored.activeTerminalId)

$terminals.subscribe(list => persistTerminals(list, $activeTerminalId.get()))
$activeTerminalId.subscribe(active => persistTerminals($terminals.get(), active))

export const $activeTerminal = computed(
  [$terminals, $activeTerminalId],
  (list, id) => list.find(term => term.id === id) ?? null
)

const newId = () =>
  globalThis.crypto?.randomUUID?.() ?? `term-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`

const terminalMatchesIdentity = (term: TerminalEntry, identity: ActiveGatewayIdentity) =>
  term.kind === 'user' &&
  (!term.profile ||
    registryBackendScopeKey(term.connectionId, normalizeProfileKey(term.profile)) ===
      registryBackendScopeKey(identity.connectionId, normalizeProfileKey(identity.profile)))

const currentTerminalIdentity = (): ActiveGatewayIdentity => $activeGatewayIdentity.get()

/** Append a fresh terminal and focus it. Captures the current cwd and gateway
 *  profile once; pass explicit values to override. Returns the id. */
export function createTerminal(
  cwd: string = $currentCwd.get(),
  identity: ActiveGatewayIdentity = currentTerminalIdentity()
): string {
  const id = newId()
  const profile = normalizeProfileKey(identity.profile)
  $terminals.set([
    ...$terminals.get(),
    {
      id,
      title: 'Terminal',
      auto: true,
      cwd,
      kind: 'user',
      ...(identity.connectionId ? { connectionId: identity.connectionId } : {}),
      profile
    }
  ])
  $activeTerminalId.set(id)

  return id
}

/** Migrate a pre-profile terminal tab to the backend that first revives it.
 *  Explicit bindings are immutable: a later rail switch must never move a live
 *  shell to another machine. */
export function bindTerminalIdentity(id: string, identity: ActiveGatewayIdentity): void {
  const profile = normalizeProfileKey(identity.profile)
  $terminals.set(
    $terminals.get().map(term =>
      term.id === id && term.kind === 'user' && !term.profile
        ? { ...term, connectionId: identity.connectionId ?? undefined, profile }
        : term
    )
  )
}

/** Bind every tab restored from the pre-identity storage schema before the
 * workspace mounts their PTY effects. This snapshots one backend identity for
 * all legacy tabs and prevents a later rail switch from restarting an inactive
 * tab on another machine. */
export function bindLegacyTerminalIdentities(identity: ActiveGatewayIdentity): void {
  const profile = normalizeProfileKey(identity.profile)
  $terminals.set(
    $terminals.get().map(term =>
      term.kind === 'user' && !term.profile
        ? { ...term, connectionId: identity.connectionId ?? undefined, profile }
        : term
    )
  )
}

// Procs we've already surfaced a tab for — so closing an agent tab doesn't
// resurrect it on the next poll while the process is still running.
const surfacedProcs = new Set<string>()

const findByProc = (procId: string) => $terminals.get().find(term => term.procId === procId)

/** Auto-surface an agent background process as a read-only tab — once. Returns
 *  the tab id, or null if it was already surfaced and the user has since closed it. */
export function ensureAgentTerminal(procId: string, title: string): string | null {
  const existing = findByProc(procId)

  if (existing) {
    return existing.id
  }

  if (surfacedProcs.has(procId)) {
    return null
  }

  surfacedProcs.add(procId)
  const id = newId()
  $terminals.set([...$terminals.get(), { id, title: title || 'agent', auto: false, cwd: '', kind: 'agent', procId }])

  return id
}

/** Open + focus an agent process's tab (the status-stack link), recreating it if
 *  the user had closed it. Opens the pane. */
export function openAgentTerminal(procId: string, title: string): void {
  surfacedProcs.add(procId)
  seedAgentTerminalCommand(procId, title)
  let id = findByProc(procId)?.id

  if (!id) {
    id = newId()
    $terminals.set([...$terminals.get(), { id, title: title || 'agent', auto: false, cwd: '', kind: 'agent', procId }])
  }

  $activeTerminalId.set(id)
  setTerminalTakeover(true)
}

/** Guarantee at least one tab exists when the pane opens.
 *  If a status-stack click already opened an agent tab, don't create a
 *  second, unrelated user shell just because the pane became visible. */
export function ensureTerminal(identity: ActiveGatewayIdentity = currentTerminalIdentity()): void {
  const list = $terminals.get()
  const active = list.find(term => term.id === $activeTerminalId.get())

  // A status-stack click deliberately surfaced an agent process. Preserve that
  // focus instead of spawning an unrelated interactive shell beside it.
  if (active?.kind === 'agent') {
    return
  }

  const match =
    (active && terminalMatchesIdentity(active, identity) ? active : null) ??
    list.find(term => terminalMatchesIdentity(term, identity))

  if (match) {
    bindTerminalIdentity(match.id, identity)
    $activeTerminalId.set(match.id)

    return
  }

  createTerminal($currentCwd.get(), identity)
}

export function selectTerminal(id: string): void {
  if ($terminals.get().some(term => term.id === id)) {
    $activeTerminalId.set(id)
  }
}

// Compare-ready form of a directory path: trimmed, trailing separators dropped
// (keeping a bare root intact) so `/repo/` and `/repo` are the same place.
const normalizePath = (value: string) => {
  const trimmed = value.trim()

  return trimmed.length > 1 ? trimmed.replace(/[\\/]+$/, '') || trimmed : trimmed
}

/** The directory a tab points at right now — the live shell cwd once observed
 *  (survives a `cd`), else the launch dir. */
const terminalCwd = (term: TerminalEntry) => normalizePath(term.restoreCwd || term.cwd)

// Session ↔ terminal linking. Entering a session whose cwd already has a user
// terminal pointed at it re-selects that tab, so the terminal pane follows the
// workspace you're in. Selection ONLY — it never creates a shell, never closes
// one, and never reveals the pane; a detached session (empty cwd) or a cwd no
// tab lives in leaves the tabs exactly where they were. `listen` (not
// `subscribe`) so boot keeps the persisted active tab.
$currentCwd.listen(cwd => {
  const target = normalizePath(cwd)
  const identity = $activeGatewayIdentity.get()

  if (!target) {
    return
  }

  const list = $terminals.get()
  const active = list.find(term => term.id === $activeTerminalId.get())

  if (active && terminalMatchesIdentity(active, identity) && terminalCwd(active) === target) {
    return
  }

  const match = list.find(term => terminalMatchesIdentity(term, identity) && terminalCwd(term) === target)

  if (match) {
    $activeTerminalId.set(match.id)
  }
})

/** Move the active tab by `direction` (+1 next / -1 prev), wrapping around. */
export function cycleTerminal(direction: 1 | -1): void {
  const list = $terminals.get()

  if (list.length < 2) {
    return
  }

  const current = Math.max(
    0,
    list.findIndex(term => term.id === $activeTerminalId.get())
  )

  $activeTerminalId.set(list[(current + direction + list.length) % list.length].id)
}

/** Drop a terminal. Focus slides to the neighbor that fills its slot; closing
 *  the last one closes the whole pane. */
export function closeTerminal(id: string): void {
  const list = $terminals.get()
  const index = list.findIndex(term => term.id === id)

  if (index < 0) {
    return
  }

  const next = list.filter(term => term.id !== id)
  $terminals.set(next)

  if ($activeTerminalId.get() === id) {
    $activeTerminalId.set((next[index] ?? next[index - 1])?.id ?? null)
  }

  if (!next.length) {
    setTerminalTakeover(false)
  }
}

/** Close the read-only agent tab mirroring a background process. The agent
 *  drives this via the desktop-gated `close_terminal` tool → `terminal.close`.
 *  The process is NOT killed — only the view is dropped; `surfacedProcs` keeps
 *  it from auto-resurfacing, and the status-stack row can reopen it on demand.
 *  No-op when no such tab exists. */
export function closeAgentTerminalByProc(procId: string): boolean {
  const term = $terminals.get().find(t => t.kind === 'agent' && t.procId === procId)

  if (!term) {
    return false
  }

  closeTerminal(term.id)

  return true
}

export function closeActiveTerminal(): void {
  const id = $activeTerminalId.get()

  if (id) {
    closeTerminal(id)
  }
}

export function closeAllTerminals(): void {
  if ($terminals.get().length === 0) {
    return
  }

  $terminals.set([])
  $activeTerminalId.set(null)
  setTerminalTakeover(false)
}

export function closeOtherTerminals(id: string): void {
  const keep = $terminals.get().find(term => term.id === id)

  if (keep) {
    $terminals.set([keep])
    $activeTerminalId.set(keep.id)
  }
}

/** Record the latest serialized scrollback for a tab so it can be replayed on
 *  the next launch. Oversized buffers are tail-trimmed to stay under the storage
 *  budget; only user tabs ever carry one. */
export function updateTerminalReviveBuffer(id: string, reviveBuffer: string): void {
  const capped =
    reviveBuffer.length > MAX_REVIVE_BUFFER_CHARS ? reviveBuffer.slice(-MAX_REVIVE_BUFFER_CHARS) : reviveBuffer

  $terminals.set(
    $terminals.get().map(term => (term.id === id && term.kind === 'user' ? { ...term, reviveBuffer: capped } : term))
  )
}

/** Record the shell's latest working directory for a tab so the next launch can
 *  restart the PTY there instead of the original launch dir. User tabs only;
 *  no-ops when the value is empty or unchanged to avoid redundant persistence. */
export function updateTerminalRestoreCwd(id: string, restoreCwd: string): void {
  const next = restoreCwd.trim()

  if (!next) {
    return
  }

  $terminals.set(
    $terminals.get().map(term => {
      if (term.id !== id || term.kind !== 'user' || term.restoreCwd === next) {
        return term
      }

      return { ...term, restoreCwd: next }
    })
  )
}

export function renameTerminal(id: string, title: string): void {
  const trimmed = title.trim()

  $terminals.set(
    $terminals.get().map(term => (term.id === id ? { ...term, title: trimmed || term.title, auto: false } : term))
  )
}

/** A live terminal reports its resolved shell; adopt it as the label only while
 *  the user hasn't named the tab themselves. */
export function reportTerminalShell(id: string, shell: string): void {
  const name = shell.trim()

  if (!name) {
    return
  }

  $terminals.set($terminals.get().map(term => (term.id === id && term.auto ? { ...term, title: name } : term)))
}
