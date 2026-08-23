import {
  backendCommandMatches,
  type BackendOwnershipEntry,
  type BackendOwnershipInspection
} from './backend-ownership'

const WINDOWS_EPOCH_TICKS = 621_355_968_000_000_000n

export interface BackendProcessSnapshot {
  command: string | null
  pid: number
  startMarkers: ReadonlySet<string>
}

interface WindowsProcessRow {
  command?: unknown
  milliseconds?: unknown
  pid?: unknown
  ticks?: unknown
}

/** One PowerShell process-table snapshot for every child + parent PID.
 *
 * Start markers intentionally come from `Get-Process.StartTime`, the same
 * provider used when a child is first claimed. `Win32_Process.CreationDate`
 * truncates to microseconds on some Windows versions, so comparing its ticks
 * with the original 100ns marker can misclassify a live child as a reused PID.
 */
export function windowsProcessSnapshotScript(pids: readonly number[]): string {
  const unique = [...new Set(pids.filter(pid => Number.isInteger(pid) && pid > 0))]

  if (!unique.length) {
    return "'[]'"
  }

  const filter = unique.map(pid => `ProcessId = ${pid}`).join(' OR ')
  const processIds = unique.join(', ')

  return [
    `$processes = @(Get-Process -Id @(${processIds}) -ErrorAction SilentlyContinue)`,
    `$rows = @(Get-CimInstance Win32_Process -Filter '${filter}' -ErrorAction Stop)`,
    '$commands = @{}',
    '$rows | ForEach-Object { $commands[[int]$_.ProcessId] = [string]$_.CommandLine }',
    '$items = @($processes | ForEach-Object {',
    '  $ticks = $_.StartTime.ToUniversalTime().Ticks',
    '  $pid = [int]$_.Id',
    '  [PSCustomObject]@{',
    '    pid = $pid',
    '    ticks = [string]$ticks',
    `    milliseconds = [string][math]::Floor(([decimal]$ticks - ${WINDOWS_EPOCH_TICKS}) / 10000)`,
    '    command = if ($commands.ContainsKey($pid)) { [string]$commands[$pid] } else { $null }',
    '  }',
    '})',
    'ConvertTo-Json -InputObject @($items) -Compress'
  ].join('; ')
}

export function parseWindowsProcessSnapshot(stdout: unknown): Map<number, BackendProcessSnapshot> {
  const text = String(stdout ?? '').trim()

  if (!text) {
    throw new Error('Windows process snapshot returned no JSON.')
  }

  const decoded: unknown = JSON.parse(text)
  const rows = Array.isArray(decoded) ? decoded : decoded && typeof decoded === 'object' ? [decoded] : []
  const snapshots = new Map<number, BackendProcessSnapshot>()

  for (const value of rows) {
    if (!value || typeof value !== 'object') {
      continue
    }

    const row = value as WindowsProcessRow
    const pid = Number(row.pid)

    if (!Number.isInteger(pid) || pid <= 0) {
      continue
    }

    const ticks = String(row.ticks ?? '')
    const milliseconds = String(row.milliseconds ?? '')
    const startMarkers = new Set<string>()

    if (/^\d+$/.test(ticks)) {
      startMarkers.add(`win:${ticks}`)
    }

    if (/^\d+$/.test(milliseconds)) {
      startMarkers.add(`winms:${milliseconds}`)
    }

    snapshots.set(pid, {
      command: typeof row.command === 'string' && row.command.trim() ? row.command : null,
      pid,
      startMarkers
    })
  }

  return snapshots
}

/**
 * Compare the durable spawn ledger against one immutable process-table view.
 * Missing rows are confirmed-dead only because the snapshot command itself
 * completed successfully; command access failures stay unknown/fail-closed.
 */
export function inspectBackendOwnershipSnapshot(
  entries: readonly BackendOwnershipEntry[],
  snapshots: ReadonlyMap<number, BackendProcessSnapshot>
): BackendOwnershipInspection[] {
  return entries.map(entry => {
    const process = snapshots.get(entry.pid)
    let identityMatches: boolean | undefined

    if (!process || !process.startMarkers.has(entry.startMarker)) {
      identityMatches = false
    } else if (process.command === null) {
      identityMatches = undefined
    } else {
      identityMatches = backendCommandMatches(process.command)
    }

    let parentMatches: boolean | undefined

    const parentPid = Number(entry.parentPid)

    if (Number.isInteger(parentPid) && parentPid > 0 && entry.parentStartMarker) {
      const parent = snapshots.get(parentPid)
      parentMatches = parent ? parent.startMarkers.has(entry.parentStartMarker) : false
    }

    return { identityMatches, parentMatches }
  })
}
