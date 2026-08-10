import { atom } from 'nanostores'

import { $terminalTakeover } from '@/app/right-sidebar/store'
import { group, groupLeafIds, isLayoutNode, type LayoutNode, split } from '@/components/pane-shell/tree/model'
import {
  $dismissedPanes,
  adoptContributedPanes,
  applyTree,
  captureLayoutStateSnapshot,
  type LayoutStateSnapshot,
  resetLayoutTree,
  restoreLayoutStateSnapshot
} from '@/components/pane-shell/tree/store'
import {
  $composerPopoutZones,
  captureComposerPopoutSnapshot,
  type ComposerPopoutSnapshot,
  isComposerPopoutSnapshot,
  reconcileComposerPopoutSnapshot,
  restoreComposerPopoutSnapshot
} from '@/store/composer-popout'
import { $panesFlipped, $rightRailActiveTabId } from '@/store/layout'
import { $reviewOpen } from '@/store/review'
import { $statusbarVisible } from '@/store/statusbar-prefs'
import { $toolViewMode, type ToolViewMode } from '@/store/tool-view'
import { isAuxiliaryWindow } from '@/store/windows'

import { $paneStates, type PaneStateSnapshot, setPaneOpen } from './panes'

export const RAN_MODE_PRESET_ID = 'ran-mode'
export const RAN_MODE_STORAGE_KEY = 'hermes.desktop.ranMode.v1'
export const RAN_MODE_STORAGE_BACKUP_KEY = `${RAN_MODE_STORAGE_KEY}.backup`
export const RAN_MODE_LOCK_NAME = 'hermes.desktop.ranMode.journal.v1'

let journalLockHeld = false
let journalTransitionPending = false
let lastJournalSequence = 0
let pendingPeerStorageEvents = 0
let peerStorageEventChain: Promise<void> = Promise.resolve()

const OWNED_STORAGE_KEYS = {
  activePreset: 'hermes.desktop.layoutPreset.active',
  composerPopoutZones: 'hermes.desktop.composerPopout.zones.v1',
  dismissedPanes: 'hermes.desktop.dismissedPanes.v1',
  layoutTree: 'hermes.desktop.layoutTree.v2',
  paneStates: 'hermes.desktop.paneStates.v1',
  panesFlipped: 'hermes.desktop.panesFlipped',
  reviewOpen: 'hermes.desktop.reviewOpen',
  rightRailActiveTab: 'hermes.desktop.rightRailActiveTab',
  statusbarVisible: 'hermes.desktop.statusbarVisible',
  terminalTakeover: 'hermes.desktop.terminalTakeover',
  toolViewTechnical: 'hermes.desktop.toolView.technical',
  userPlacedPanes: 'hermes.desktop.userPlacedPanes.v1'
} as const

const OWNED_STORAGE_KEY_LIST = Object.values(OWNED_STORAGE_KEYS)

export const RAN_MODE_TREE = split(
  'row',
  [
    group(['sessions'], { id: 'ran-mode-sessions', minimized: true }),
    group(['workspace'], { id: 'ran-mode-workspace' })
  ],
  [0.08, 1],
  'ran-mode-root'
)

interface RanModeSnapshotV1 {
  composerPopout: ComposerPopoutSnapshot
  layout: LayoutStateSnapshot
  paneStates: Record<string, PaneStateSnapshot>
  reviewOpen: boolean
  statusbarVisible: boolean
  terminalTakeover: boolean
  toolViewMode: ToolViewMode
}

type RanModeExit =
  | { kind: 'preset'; presetId: string; tree: LayoutNode }
  | { kind: 'reset' }

interface RanModeLiveRecordV1 {
  enabled: true
  journalSequence?: number
  ownedStateFingerprint?: string
  phase: 'active' | 'applying'
  snapshot: RanModeSnapshotV1
  transactionId: string
  version: 1
}

interface RanModeSettledRecordV1 {
  completed: boolean
  enabled: false
  exit?: RanModeExit
  journalSequence?: number
  phase: 'inactive' | 'leaving'
  restorePolicy: 'conditional' | 'force'
  settlementFingerprint?: string
  snapshot: RanModeSnapshotV1
  transactionId: string
  version: 1
}

type RanModeRecordV1 = RanModeLiveRecordV1 | RanModeSettledRecordV1

interface RanModeJournalEnvelopeV2 {
  checksum: string
  payload: null | string
  sequence: number
  version: 2
}

const isObject = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null

function isPaneState(value: unknown): value is PaneStateSnapshot {
  return (
    isObject(value) &&
    typeof value.open === 'boolean' &&
    (value.widthOverride === undefined || typeof value.widthOverride === 'number') &&
    (value.heightOverride === undefined || typeof value.heightOverride === 'number')
  )
}

function isLayoutSnapshot(value: unknown): value is LayoutStateSnapshot {
  return (
    isObject(value) &&
    typeof value.activePresetId === 'string' &&
    isLayoutNode(value.tree) &&
    Array.isArray(value.userPlacedPaneIds) &&
    value.userPlacedPaneIds.every(id => typeof id === 'string')
  )
}

function isSnapshot(value: unknown): value is RanModeSnapshotV1 {
  return (
    isObject(value) &&
    isComposerPopoutSnapshot(value.composerPopout) &&
    isLayoutSnapshot(value.layout) &&
    isObject(value.paneStates) &&
    Object.values(value.paneStates).every(isPaneState) &&
    typeof value.reviewOpen === 'boolean' &&
    typeof value.statusbarVisible === 'boolean' &&
    typeof value.terminalTakeover === 'boolean' &&
    (value.toolViewMode === 'product' || value.toolViewMode === 'technical')
  )
}

function isExit(value: unknown): value is RanModeExit {
  if (!isObject(value) || (value.kind !== 'preset' && value.kind !== 'reset')) {
    return false
  }

  return value.kind === 'reset' || (typeof value.presetId === 'string' && isLayoutNode(value.tree))
}

function isRanModeRecord(value: unknown): value is RanModeRecordV1 {
  if (!isObject(value)) {
    return false
  }

  const journalSequence = value.journalSequence

  if (
    value.version !== 1 ||
    (journalSequence !== undefined &&
      (typeof journalSequence !== 'number' || !Number.isSafeInteger(journalSequence) || journalSequence < 0)) ||
    typeof value.transactionId !== 'string' ||
    value.transactionId.length === 0 ||
    !isSnapshot(value.snapshot)
  ) {
    return false
  }

  if (value.enabled === true) {
    return (
      (value.ownedStateFingerprint === undefined || typeof value.ownedStateFingerprint === 'string') &&
      (value.phase === 'active' || value.phase === 'applying')
    )
  }

  if (
    value.enabled !== false ||
    typeof value.completed !== 'boolean' ||
    (value.settlementFingerprint !== undefined && typeof value.settlementFingerprint !== 'string') ||
    (value.phase !== 'inactive' && value.phase !== 'leaving') ||
    (value.restorePolicy !== 'conditional' && value.restorePolicy !== 'force')
  ) {
    return false
  }

  return value.phase === 'inactive' ? value.exit === undefined : isExit(value.exit)
}

interface RecordReadResult {
  ok: boolean
  record: RanModeRecordV1 | null
  sequence: number
  writable?: boolean
}

function parseRecord(raw: null | string): RanModeRecordV1 | null {
  if (raw === null) {
    return null
  }

  try {
    const value: unknown = JSON.parse(raw)

    return isRanModeRecord(value) ? value : null
  } catch {
    return null
  }
}

function journalChecksum(sequence: number, payload: null | string): string {
  const source = `${sequence}:${payload ?? '<cleared>'}`
  let hash = 2166136261

  for (let index = 0; index < source.length; index += 1) {
    hash ^= source.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }

  return (hash >>> 0).toString(16).padStart(8, '0')
}

function serializeEnvelope(sequence: number, payload: null | string): string {
  return JSON.stringify({ checksum: journalChecksum(sequence, payload), payload, sequence, version: 2 })
}

function nextJournalSequence(current: number): number {
  // Preserve ordering after cleanup removes both slots: wall-clock high bits
  // prevent a delayed predecessor from outranking the next generation, while
  // the local/current increments cover clock stalls and same-tick transitions.
  lastJournalSequence = Math.max(lastJournalSequence + 1, current + 1, Date.now() * 1024)

  return lastJournalSequence
}

function parseEnvelope(raw: null | string): RanModeJournalEnvelopeV2 | null {
  if (raw === null) {
    return null
  }

  try {
    const value: unknown = JSON.parse(raw)

    if (!isObject(value)) {
      return null
    }

    const sequence = value.sequence

    if (
      value.version !== 2 ||
      typeof sequence !== 'number' ||
      !Number.isSafeInteger(sequence) ||
      sequence < 1 ||
      (value.payload !== null && typeof value.payload !== 'string') ||
      typeof value.checksum !== 'string' ||
      value.checksum !== journalChecksum(value.sequence as number, value.payload as null | string)
    ) {
      return null
    }

    if (value.payload !== null) {
      const record = parseRecord(value.payload)

      if (!record || record.journalSequence !== value.sequence) {
        return null
      }
    }

    return value as unknown as RanModeJournalEnvelopeV2
  } catch {
    return null
  }
}

function parseStorageRecord(raw: null | string): RanModeRecordV1 | null {
  const direct = parseRecord(raw)

  if (direct) {
    return direct
  }

  const envelope = parseEnvelope(raw)

  return envelope ? parseRecord(envelope.payload) : null
}

function removeExactStorageValue(key: string, expected: null | string): boolean {
  if (!journalLockHeld) {
    return false
  }

  try {
    if (window.localStorage.getItem(key) !== expected) {
      return false
    }

    window.localStorage.removeItem(key)

    return window.localStorage.getItem(key) === null
  } catch {
    return false
  }
}

function readRecord(): RecordReadResult {
  try {
    const primaryRaw = window.localStorage.getItem(RAN_MODE_STORAGE_KEY)
    const backupRaw = window.localStorage.getItem(RAN_MODE_STORAGE_BACKUP_KEY)
    const primary = parseRecord(primaryRaw)
    const backup = parseEnvelope(backupRaw)
    const primaryInvalid = primaryRaw !== null && !primary
    const backupInvalid = backupRaw !== null && !backup

    if (primaryInvalid && !backup) {
      if (!journalLockHeld || !removeExactStorageValue(RAN_MODE_STORAGE_KEY, primaryRaw)) {
        return { ok: false, record: null, sequence: 0 }
      }
    }

    if (backupInvalid) {
      if (!primary || (!journalLockHeld && primaryRaw === null)) {
        return { ok: false, record: null, sequence: primary?.journalSequence ?? 0 }
      }

      if (journalLockHeld) {
        removeExactStorageValue(RAN_MODE_STORAGE_BACKUP_KEY, backupRaw)
      }
    }

    const primarySequence = primary?.journalSequence ?? 0

    if (backup && (!primary || backup.sequence >= primarySequence)) {
      const backupRecord = parseRecord(backup.payload)

      if (journalLockHeld) {
        let repaired = false

        try {
          repaired =
            backup.payload === null
              ? primaryRaw === null || removeExactStorageValue(RAN_MODE_STORAGE_KEY, primaryRaw)
              : (() => {
                  window.localStorage.setItem(RAN_MODE_STORAGE_KEY, backup.payload)

                  return window.localStorage.getItem(RAN_MODE_STORAGE_KEY) === backup.payload
                })()
        } catch {
          // The checksummed successor remains authoritative in the backup slot.
        }

        if (!repaired) {
          return { ok: true, record: backupRecord, sequence: backup.sequence, writable: false }
        }

        removeExactStorageValue(RAN_MODE_STORAGE_BACKUP_KEY, backupRaw)
      }

      return { ok: true, record: backupRecord, sequence: backup.sequence }
    }

    if (backup && journalLockHeld) {
      removeExactStorageValue(RAN_MODE_STORAGE_BACKUP_KEY, backupRaw)
    }

    return { ok: true, record: primary, sequence: primarySequence }
  } catch {
    return { ok: false, record: null, sequence: 0 }
  }
}

/**
 * Stage each successor in a checksummed backup slot before touching the
 * primary record. A corrupt stage leaves the predecessor intact; a corrupt
 * primary mirror leaves the validated successor available for restart repair.
 */
function persistRecord(record: RanModeRecordV1): boolean {
  if (!journalLockHeld) {
    return false
  }

  const authority = readRecord()

  if (!authority.ok || authority.writable === false) {
    return false
  }

  const sequence = nextJournalSequence(authority.sequence)
  const durableRecord = { ...record, journalSequence: sequence } as RanModeRecordV1
  const serialized = JSON.stringify(durableRecord)
  const envelope = serializeEnvelope(sequence, serialized)

  try {
    window.localStorage.setItem(RAN_MODE_STORAGE_BACKUP_KEY, envelope)

    if (window.localStorage.getItem(RAN_MODE_STORAGE_BACKUP_KEY) !== envelope) {
      removeExactStorageValue(RAN_MODE_STORAGE_BACKUP_KEY, window.localStorage.getItem(RAN_MODE_STORAGE_BACKUP_KEY))

      return false
    }

    Object.assign(record, { journalSequence: sequence })
  } catch {
    return false
  }

  try {
    window.localStorage.setItem(RAN_MODE_STORAGE_KEY, serialized)

    if (window.localStorage.getItem(RAN_MODE_STORAGE_KEY) === serialized) {
      removeExactStorageValue(RAN_MODE_STORAGE_BACKUP_KEY, envelope)
    }
  } catch {
    // The validated backup is already the authoritative successor. Leave it in
    // place so restart can repair the primary mirror without losing the phase.
  }

  return true
}

/**
 * Settled records are safe tombstones. Their durable `enabled:false` state is
 * already sufficient for restart safety, so removal is cleanup only. A failed
 * or ambiguous removal leaves a harmless record that startup can replay.
 */
function removeSettledRecord(record: RanModeSettledRecordV1): void {
  if (!journalLockHeld) {
    return
  }

  try {
    const authority = readRecord()

    if (
      !authority.ok ||
      authority.writable === false ||
      !authority.record ||
      authority.record.enabled ||
      !authority.record.completed ||
      authority.record.transactionId !== record.transactionId
    ) {
      return
    }

    const cleared = serializeEnvelope(nextJournalSequence(authority.sequence), null)

    window.localStorage.setItem(RAN_MODE_STORAGE_BACKUP_KEY, cleared)

    if (window.localStorage.getItem(RAN_MODE_STORAGE_BACKUP_KEY) !== cleared) {
      removeExactStorageValue(RAN_MODE_STORAGE_BACKUP_KEY, window.localStorage.getItem(RAN_MODE_STORAGE_BACKUP_KEY))

      return
    }

    const primary = window.localStorage.getItem(RAN_MODE_STORAGE_KEY)

    if (primary === null || removeExactStorageValue(RAN_MODE_STORAGE_KEY, primary)) {
      removeExactStorageValue(RAN_MODE_STORAGE_BACKUP_KEY, cleared)
    }
  } catch {
    // Keep the durable disabled tombstone. Never reactivate Ran Mode here.
  }
}

const initialRead = isAuxiliaryWindow() ? { ok: true, record: null, sequence: 0 } : readRecord()
let currentRecord = initialRead.ok ? initialRead.record : null

export const $ranModeEnabled = atom(Boolean(currentRecord?.enabled))

function createTransactionId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function captureSnapshot(): RanModeSnapshotV1 {
  return {
    composerPopout: captureComposerPopoutSnapshot(),
    layout: captureLayoutStateSnapshot(),
    paneStates: structuredClone($paneStates.get()),
    reviewOpen: $reviewOpen.get(),
    statusbarVisible: $statusbarVisible.get(),
    terminalTakeover: $terminalTakeover.get(),
    toolViewMode: $toolViewMode.get()
  }
}

function applyLayoutOwnedState(): void {
  applyTree(structuredClone(RAN_MODE_TREE), RAN_MODE_PRESET_ID)
  setPaneOpen('chat-sidebar', true)
  setPaneOpen('file-browser', false)
  $reviewOpen.set(false)
  $terminalTakeover.set(false)
}

function applyUserOwnedDefaults(force: boolean): void {
  if (force || $statusbarVisible.get() === false) {
    $statusbarVisible.set(false)
  }

  if (force || $toolViewMode.get() === 'product') {
    $toolViewMode.set('product')
  }
}

function shouldForceUserOwnedRestore(record: RanModeRecordV1): boolean {
  return record.enabled ? record.phase === 'applying' : record.restorePolicy === 'force'
}

function restoreUserOwnedState(record: RanModeRecordV1): void {
  if (shouldForceUserOwnedRestore(record) || $statusbarVisible.get() === false) {
    $statusbarVisible.set(record.snapshot.statusbarVisible)
  }

  if (shouldForceUserOwnedRestore(record) || $toolViewMode.get() === 'product') {
    $toolViewMode.set(record.snapshot.toolViewMode)
  }
}

function restoreNonLayoutState(record: RanModeRecordV1): void {
  // Snapshot-owned panes return exactly; panes registered while the mode was
  // active are user/system-owned and remain present.
  $paneStates.set({ ...$paneStates.get(), ...structuredClone(record.snapshot.paneStates) })
  $reviewOpen.set(record.snapshot.reviewOpen)
  $terminalTakeover.set(record.snapshot.terminalTakeover)
  restoreUserOwnedState(record)
  restoreComposerPopoutSnapshot(record.snapshot.composerPopout)
}

function restoreLayoutOwnedState(record: RanModeRecordV1): void {
  restoreLayoutStateSnapshot(record.snapshot.layout)
  // Registry entries can arrive while Ran Mode is active (plugins, previews,
  // session tiles). Re-adopt after restoring the old tree so none disappear.
  adoptContributedPanes()
  restoreNonLayoutState(record)
}

function applyExit(exit: RanModeExit): boolean {
  if (exit.kind === 'reset') {
    return resetLayoutTree()
  } else {
    applyTree(structuredClone(exit.tree), exit.presetId)
  }

  return false
}

function finishSettledRecord(record: RanModeSettledRecordV1): boolean {
  let resetRequiresPersistedTree = false

  if (record.phase === 'inactive') {
    restoreLayoutOwnedState(record)
  } else {
    restoreNonLayoutState(record)
    resetRequiresPersistedTree = applyExit(record.exit!)
  }

  $ranModeEnabled.set(false)

  return resetRequiresPersistedTree
}

function verifyCurrentOwnedStatePersistence(record: RanModeSettledRecordV1, resetRequiresPersistedTree: boolean): boolean {
  const layout = captureLayoutStateSnapshot()
  const serializedLayoutTree = JSON.stringify(layout.tree)
  const resetSettlement = record.phase === 'leaving' && record.exit?.kind === 'reset'
  let persistedComposerPopoutZones: string | null
  let persistedLayoutTree: string | null

  try {
    persistedComposerPopoutZones = window.localStorage.getItem(OWNED_STORAGE_KEYS.composerPopoutZones)
    persistedLayoutTree = window.localStorage.getItem(OWNED_STORAGE_KEYS.layoutTree)
  } catch {
    return false
  }

  const expectedLayoutTree = resetSettlement ? (resetRequiresPersistedTree ? serializedLayoutTree : null) : serializedLayoutTree

  const expectedComposerPopout =
    record.phase === 'inactive'
      ? record.snapshot.composerPopout
      : reconcileComposerPopoutSnapshot(record.snapshot.composerPopout, groupLeafIds(layout.tree))

  if (
    persistedLayoutTree !== expectedLayoutTree ||
    persistedComposerPopoutZones !== expectedComposerPopout.storageValue ||
    JSON.stringify($composerPopoutZones.get()) !== JSON.stringify(expectedComposerPopout.zones)
  ) {
    return false
  }

  const dismissedPanes = $dismissedPanes.get()
  const expectedDismissedPanes = dismissedPanes.size === 0 ? null : JSON.stringify([...dismissedPanes])

  const expected = new Map<string, null | string>([
    [OWNED_STORAGE_KEYS.activePreset, layout.activePresetId],
    [OWNED_STORAGE_KEYS.composerPopoutZones, expectedComposerPopout.storageValue],
    [OWNED_STORAGE_KEYS.dismissedPanes, expectedDismissedPanes],
    [OWNED_STORAGE_KEYS.layoutTree, expectedLayoutTree],
    [OWNED_STORAGE_KEYS.paneStates, JSON.stringify($paneStates.get())],
    [OWNED_STORAGE_KEYS.panesFlipped, String($panesFlipped.get())],
    [OWNED_STORAGE_KEYS.reviewOpen, String($reviewOpen.get())],
    [OWNED_STORAGE_KEYS.rightRailActiveTab, $rightRailActiveTabId.get() ?? ''],
    [OWNED_STORAGE_KEYS.statusbarVisible, String($statusbarVisible.get())],
    [OWNED_STORAGE_KEYS.terminalTakeover, String($terminalTakeover.get())],
    [OWNED_STORAGE_KEYS.toolViewTechnical, String($toolViewMode.get() === 'technical')],
    [
      OWNED_STORAGE_KEYS.userPlacedPanes,
      layout.userPlacedPaneIds.length === 0 ? null : JSON.stringify(layout.userPlacedPaneIds)
    ]
  ])

  try {
    return [...expected].every(([key, value]) => window.localStorage.getItem(key) === value)
  } catch {
    return false
  }
}

function captureOwnedStorageFingerprint(): string | null {
  try {
    return JSON.stringify(OWNED_STORAGE_KEY_LIST.map(key => [key, window.localStorage.getItem(key)]))
  } catch {
    return null
  }
}

function withCurrentOwnedStateFingerprint(record: RanModeLiveRecordV1): RanModeLiveRecordV1 {
  const ownedStateFingerprint = captureOwnedStorageFingerprint()

  return ownedStateFingerprint === null ? record : { ...record, ownedStateFingerprint }
}

type CompletionResult =
  | { status: 'completed'; record: RanModeSettledRecordV1 }
  | { status: 'failed' }
  | { status: 'superseded'; record: RanModeRecordV1 | null }

function classifyCompletionOwnership(record: RanModeSettledRecordV1): CompletionResult | null {
  const durable = readRecord()

  if (!durable.ok) {
    return { status: 'failed' }
  }

  if (durable.record?.transactionId !== record.transactionId || durable.record.enabled) {
    return { record: durable.record, status: 'superseded' }
  }

  if (durable.record.completed) {
    return { record: durable.record, status: 'completed' }
  }

  return null
}

function completeSettledRecord(record: RanModeSettledRecordV1): CompletionResult {
  if (record.completed) {
    return { record, status: 'completed' }
  }

  const before = classifyCompletionOwnership(record)

  if (before) {
    return before
  }

  let resetRequiresPersistedTree: boolean

  try {
    resetRequiresPersistedTree = finishSettledRecord(record)
  } catch {
    // The durable disabled tombstone is the retry contract. Never mark it
    // complete when any restore/apply step failed; fail-closed recovery keeps
    // the renderer in Ran Mode until a later initialize can retry the intent.
    return { status: 'failed' }
  }

  if (!verifyCurrentOwnedStatePersistence(record, resetRequiresPersistedTree)) {
    // The underlying preference stores are intentionally best-effort and
    // swallow quota/permission errors. Do not complete until every owned value
    // reads back exactly; the disabled tombstone remains the restart retry.
    return { status: 'failed' }
  }

  const settlementFingerprint = captureOwnedStorageFingerprint()

  if (settlementFingerprint === null) {
    return { status: 'failed' }
  }

  const completed: RanModeSettledRecordV1 = { ...record, completed: true, settlementFingerprint }

  const afterSettlement = classifyCompletionOwnership(record)

  if (afterSettlement) {
    return afterSettlement
  }

  return persistRecord(completed) ? { record: completed, status: 'completed' } : { status: 'failed' }
}

function enterFailClosedRecovery(record: RanModeSettledRecordV1): void {
  // Storage cannot confirm the disabled outcome. Keep the renderer visibly in
  // Ran Mode and block all exits/layout choices until restart can replay the
  // durable tombstone. The snapshot itself remains intact.
  applyLayoutOwnedState()
  applyUserOwnedDefaults(true)

  // Never overwrite the durable exit intent with an active record. The
  // in-memory Ran state is only a fail-closed guard; restart must finish the
  // preserved tombstone, not re-enter the mode.
  currentRecord = record
  $ranModeEnabled.set(true)
}

function reconcileSupersedingRecord(record: RanModeRecordV1 | null): void {
  if (record?.enabled) {
    $ranModeEnabled.set(true)
    applyLayoutOwnedState()
    applyUserOwnedDefaults(record.phase === 'applying')
    currentRecord = withCurrentOwnedStateFingerprint(record)
  } else if (record && !record.completed) {
    currentRecord = record
    enterFailClosedRecovery(record)
  } else {
    currentRecord = null
    $ranModeEnabled.set(false)
  }
}

function settleLiveRecord(record: RanModeLiveRecordV1, exit?: RanModeExit): boolean {
  const settled: RanModeSettledRecordV1 = {
    completed: false,
    enabled: false,
    ...(exit ? { exit, phase: 'leaving' as const } : { phase: 'inactive' as const }),
    restorePolicy: record.phase === 'applying' ? 'force' : 'conditional',
    snapshot: record.snapshot,
    transactionId: record.transactionId,
    version: 1
  }

  // Commit the disabled tombstone first. A crash from this point converges to
  // the intended disable/reset/preset choice during startup.
  if (!persistRecord(settled)) {
    return false
  }

  currentRecord = settled
  const completion = completeSettledRecord(settled)

  if (completion.status === 'failed') {
    enterFailClosedRecovery(settled)

    return false
  }

  if (completion.status === 'superseded') {
    reconcileSupersedingRecord(completion.record)

    return false
  }

  currentRecord = completion.record
  removeSettledRecord(completion.record)
  currentRecord = null

  return true
}

function enableRanModeUnlocked(): boolean {
  if (isAuxiliaryWindow()) {
    return false
  }

  const persisted = readRecord()

  if (!persisted.ok) {
    return false
  }

  if (persisted.record?.enabled) {
    $ranModeEnabled.set(true)
    applyLayoutOwnedState()
    applyUserOwnedDefaults(persisted.record.phase === 'applying')
    currentRecord = withCurrentOwnedStateFingerprint(persisted.record)

    return false
  }

  if (persisted.record) {
    const completion = completeSettledRecord(persisted.record)

    if (completion.status === 'failed') {
      enterFailClosedRecovery(persisted.record)

      return false
    }

    if (completion.status === 'superseded') {
      reconcileSupersedingRecord(completion.record)

      return false
    }

    removeSettledRecord(completion.record)
  }

  const applying: RanModeLiveRecordV1 = {
    enabled: true,
    phase: 'applying',
    snapshot: captureSnapshot(),
    transactionId: createTransactionId(),
    version: 1
  }

  // Write the recovery payload before changing any owned preference. A crash
  // during apply leaves a recoverable "applying" transaction, never a lost
  // baseline.
  if (!persistRecord(applying)) {
    return false
  }

  currentRecord = applying
  $ranModeEnabled.set(true)
  applyLayoutOwnedState()
  applyUserOwnedDefaults(true)

  const active = withCurrentOwnedStateFingerprint({ ...applying, phase: 'active' })

  if (persistRecord(active)) {
    currentRecord = active
  }

  return true
}

/** Reconcile a persisted transaction after Desktop startup/restart. */
function initializeRanModeUnlocked(): boolean {
  if (isAuxiliaryWindow()) {
    $ranModeEnabled.set(false)

    return false
  }

  const persisted = readRecord()

  if (!persisted.ok) {
    return false
  }

  currentRecord = persisted.record

  if (!currentRecord) {
    $ranModeEnabled.set(false)

    return false
  }

  if (!currentRecord.enabled) {
    const completion = completeSettledRecord(currentRecord)

    if (completion.status === 'failed') {
      enterFailClosedRecovery(currentRecord)

      return false
    }

    if (completion.status === 'superseded') {
      reconcileSupersedingRecord(completion.record)

      return Boolean(completion.record?.enabled)
    }

    removeSettledRecord(completion.record)
    currentRecord = null

    return false
  }

  const forceUserOwnedDefaults = currentRecord.phase === 'applying'
  $ranModeEnabled.set(true)
  applyLayoutOwnedState()
  applyUserOwnedDefaults(forceUserOwnedDefaults)

  const active = withCurrentOwnedStateFingerprint({ ...currentRecord, phase: 'active' })

  if (currentRecord.phase !== 'active') {
    if (persistRecord(active)) {
      currentRecord = active
    }
  } else {
    currentRecord = active
  }

  return true
}

function disableRanModeUnlocked(): boolean {
  if (isAuxiliaryWindow()) {
    return false
  }

  const persisted = readRecord()

  if (!persisted.ok) {
    return false
  }

  if (!persisted.record?.enabled) {
    if ($ranModeEnabled.get() && persisted.record && !persisted.record.completed) {
      return false
    }

    currentRecord = persisted.record
    $ranModeEnabled.set(false)

    return false
  }

  return settleLiveRecord(persisted.record)
}

/** Apply an explicit preset choice as the durable exit outcome. */
function leaveRanModeForLayoutChangeUnlocked(presetId: string, tree: LayoutNode): boolean {
  if (isAuxiliaryWindow()) {
    return false
  }

  const persisted = readRecord()

  return Boolean(
    persisted.ok &&
      persisted.record?.enabled &&
      settleLiveRecord(persisted.record, { kind: 'preset', presetId, tree: structuredClone(tree) })
  )
}

/** Reset is an explicit user choice, not a hidden Ran Mode rollback. */
function resetLayoutFromRanModeUnlocked(): boolean {
  if (isAuxiliaryWindow()) {
    return false
  }

  const persisted = readRecord()

  if (!persisted.ok) {
    return false
  }

  if (persisted.record?.enabled) {
    return settleLiveRecord(persisted.record, { kind: 'reset' })
  }

  if (persisted.record && !persisted.record.completed) {
    return false
  }

  resetLayoutTree()

  return true
}

function applyLayoutPresetWithRanModeUnlocked(presetId: string, tree: LayoutNode): boolean {
  if (isAuxiliaryWindow()) {
    return false
  }

  const persisted = readRecord()

  if (!persisted.ok) {
    return false
  }

  if (presetId === RAN_MODE_PRESET_ID) {
    return enableRanModeUnlocked()
  }

  if (persisted.record?.enabled) {
    return settleLiveRecord(persisted.record, {
      kind: 'preset',
      presetId,
      tree: structuredClone(tree)
    })
  }

  if (persisted.record && !persisted.record.completed) {
    return false
  }

  if (persisted.record) {
    removeSettledRecord(persisted.record)
  }

  currentRecord = null
  $ranModeEnabled.set(false)
  applyTree(structuredClone(tree), presetId)

  return true
}

export function isRanModeRecoveryIncomplete(): boolean {
  if (isAuxiliaryWindow()) {
    return true
  }

  if (journalTransitionPending || pendingPeerStorageEvents > 0) {
    return true
  }

  const persisted = readRecord()

  return !persisted.ok || Boolean(persisted.record && !persisted.record.enabled && !persisted.record.completed)
}

async function runJournalTransition(operation: () => boolean): Promise<boolean> {
  if (isAuxiliaryWindow() || journalTransitionPending || pendingPeerStorageEvents > 0) {
    return false
  }

  const locks = globalThis.navigator?.locks

  if (!locks) {
    return false
  }

  journalTransitionPending = true

  try {
    return await locks.request(RAN_MODE_LOCK_NAME, { mode: 'exclusive' }, async () => {
      journalLockHeld = true

      try {
        return operation()
      } finally {
        journalLockHeld = false
      }
    })
  } catch {
    return false
  } finally {
    journalTransitionPending = false
  }
}

async function runPeerJournalTransition(operation: () => boolean): Promise<boolean> {
  const locks = globalThis.navigator?.locks

  if (!locks) {
    return false
  }

  try {
    return await locks.request(RAN_MODE_LOCK_NAME, { mode: 'exclusive' }, async () => {
      journalLockHeld = true

      try {
        return operation()
      } finally {
        journalLockHeld = false
      }
    })
  } catch {
    return false
  }
}

export function enableRanMode(): Promise<boolean> {
  return runJournalTransition(enableRanModeUnlocked)
}

export function initializeRanMode(): Promise<boolean> {
  return runJournalTransition(initializeRanModeUnlocked)
}

export function disableRanMode(): Promise<boolean> {
  return runJournalTransition(disableRanModeUnlocked)
}

export function leaveRanModeForLayoutChange(presetId: string, tree: LayoutNode): Promise<boolean> {
  return runJournalTransition(() => leaveRanModeForLayoutChangeUnlocked(presetId, tree))
}

export function resetLayoutFromRanMode(): Promise<boolean> {
  return runJournalTransition(resetLayoutFromRanModeUnlocked)
}

export function applyLayoutPresetWithRanMode(presetId: string, tree: LayoutNode): Promise<boolean> {
  return runJournalTransition(() => applyLayoutPresetWithRanModeUnlocked(presetId, tree))
}

export function toggleRanMode(): Promise<boolean> {
  return $ranModeEnabled.get() ? disableRanMode() : enableRanMode()
}

function applyPeerSettlement(record: RanModeSettledRecordV1): void {
  if (currentRecord?.transactionId !== record.transactionId || !currentRecord.enabled) {
    return
  }

  currentRecord = record

  try {
    // Peer renderers apply the owner-written intent locally but never advance
    // or remove its durable record.
    finishSettledRecord(record)
  } catch {
    enterFailClosedRecovery(record)

    return
  }

  currentRecord = null
}

interface RanModeStorageChange {
  newValue: string | null
  oldValue: string | null
}

function durableLayoutMatchesSettlement(record: RanModeRecordV1): boolean {
  const expectedPreset =
    record.enabled
      ? RAN_MODE_PRESET_ID
      : record.phase === 'leaving'
        ? record.exit?.kind === 'preset'
          ? record.exit.presetId
          : 'default'
        : record.snapshot.layout.activePresetId

  const expectedTree =
    record.enabled
      ? JSON.stringify(captureLayoutStateSnapshot().tree)
      : record.phase === 'leaving'
        ? record.exit?.kind === 'reset'
          ? null
          : JSON.stringify(record.exit?.tree)
        : JSON.stringify(record.snapshot.layout.tree)

  try {
    const durablePreset = window.localStorage.getItem(OWNED_STORAGE_KEYS.activePreset)
    const durableTree = window.localStorage.getItem(OWNED_STORAGE_KEYS.layoutTree)

    return durablePreset === expectedPreset && durableTree === expectedTree
  } catch {
    return false
  }
}

function durableOwnedStateMatchesSettlement(record: RanModeRecordV1): boolean {
  if (record.enabled || !record.completed || typeof record.settlementFingerprint !== 'string') {
    return false
  }

  return captureOwnedStorageFingerprint() === record.settlementFingerprint
}

function handlePeerStorageChangeUnlocked(event: RanModeStorageChange): boolean {
  let next = parseStorageRecord(event.newValue)
  let durable: RanModeRecordV1 | null

  const durableRead = readRecord()

  if (!durableRead.ok) {
    // Ambiguous storage is not authority to mutate renderer state.
    return false
  }

  durable = durableRead.record

  if (next && durable) {
    if (durable.transactionId !== next.transactionId) {
      return false
    }

    // localStorage is the current durable authority. Storage events can lag
    // behind later writes for the same transaction, so reconcile the durable
    // phase/intent rather than replaying an older event payload.
    next = durable
  }

  if (next && !durable) {
    // The event lost durable authority before this renderer acquired the lock.
    // Replaying a stale settlement can overwrite any later owned preference.
    // Only a completed record whose full durable fingerprint still matches may
    // replay; otherwise clear local ownership without writing any preference.
    if (
      next.enabled ||
      currentRecord?.transactionId !== next.transactionId ||
      !currentRecord.enabled ||
      !durableOwnedStateMatchesSettlement(next)
    ) {
      return false
    }

    applyPeerSettlement(next)

    return true
  }

  if (next && !next.enabled && next.completed) {
    // Completed tombstones are inert for an already-off renderer. A peer that
    // is still actively showing this exact transaction must apply the outcome
    // locally once; this never mutates the durable tombstone.
    applyPeerSettlement(next)

    return true
  }

  if (next?.enabled) {
    $ranModeEnabled.set(true)
    applyLayoutOwnedState()
    applyUserOwnedDefaults(next.phase === 'applying')
    currentRecord = withCurrentOwnedStateFingerprint(next)

    return true
  }

  if (next) {
    if (next.completed) {
      applyPeerSettlement(next)

      return true
    }

    if (currentRecord?.transactionId !== next.transactionId || !currentRecord.enabled) {
      return false
    }

    applyPeerSettlement(next)

    return true
  }

  // Backward/failure fallback: both completed and live records require a full
  // owned-state fingerprint. The active-layout check is retained as a second
  // guard against a stale live removal replaying over newer preference bytes.
  const previous = parseStorageRecord(event.oldValue)

  if (durable || !previous || currentRecord?.transactionId !== previous.transactionId) {
    return false
  }

  const previousStillOwnsDurableState = previous.enabled
    ? durableLayoutMatchesSettlement(previous) &&
      typeof previous.ownedStateFingerprint === 'string' &&
      captureOwnedStorageFingerprint() === previous.ownedStateFingerprint
    : durableOwnedStateMatchesSettlement(previous)

  if (!previousStillOwnsDurableState) {
    currentRecord = null
    $ranModeEnabled.set(false)

    return false
  }

  if (previous.enabled && currentRecord.enabled) {
    try {
      restoreLayoutOwnedState(previous)
    } catch {
      return false
    }
  } else if (!previous.enabled && previous.completed && currentRecord.enabled) {
    applyPeerSettlement(previous)
  } else if (!previous.enabled && !previous.completed && currentRecord.enabled) {
    try {
      finishSettledRecord(previous)
    } catch {
      enterFailClosedRecovery(previous)

      return false
    }
  }

  currentRecord = null
  $ranModeEnabled.set(false)

  return true
}

function handlePeerStorageChange(event: StorageEvent): void {
  if ((event.key !== RAN_MODE_STORAGE_KEY && event.key !== RAN_MODE_STORAGE_BACKUP_KEY) || isAuxiliaryWindow()) {
    return
  }

  const change: RanModeStorageChange = { newValue: event.newValue, oldValue: event.oldValue }

  // Storage mutations from one completed transition emit several ordered
  // events (incomplete, completed, clear-marker, removal). Never drop later
  // events merely because an earlier one is waiting for the Web Lock: serialize
  // the complete stream and re-read durable authority inside every callback.
  pendingPeerStorageEvents += 1

  const queued = peerStorageEventChain.then(() =>
    runPeerJournalTransition(() => handlePeerStorageChangeUnlocked(change))
  )

  peerStorageEventChain = queued.then(
    () => undefined,
    () => undefined
  )

  void queued.then(
    () => {
      pendingPeerStorageEvents -= 1
    },
    () => {
      pendingPeerStorageEvents -= 1
    }
  )
}

const STORAGE_LISTENER_KEY = '__hermesRanModeStorageListener__'
type RanModeWindow = Window & { [STORAGE_LISTENER_KEY]?: (event: StorageEvent) => void }

if (typeof window !== 'undefined') {
  const ranWindow = window as RanModeWindow
  const previousListener = ranWindow[STORAGE_LISTENER_KEY]

  if (previousListener) {
    window.removeEventListener('storage', previousListener)
  }

  ranWindow[STORAGE_LISTENER_KEY] = handlePeerStorageChange
  window.addEventListener('storage', handlePeerStorageChange)
}
