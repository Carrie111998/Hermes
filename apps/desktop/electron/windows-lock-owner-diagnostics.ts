import { execFileSync, type ExecFileSyncOptionsWithStringEncoding } from 'node:child_process'
import path from 'node:path'

export interface WindowsProcessInventoryEntry {
  pid: number
  parentPid: number
  executableName: string
  executablePath: string | null
}

export type LockOwnerOwnership = 'desktop-owned' | 'desktop-descendant' | 'external'

export interface WindowsLockOwnerCandidate extends WindowsProcessInventoryEntry {
  ownership: LockOwnerOwnership
}

interface RawWindowsProcess {
  ProcessId?: unknown
  ParentProcessId?: unknown
  Name?: unknown
  ExecutablePath?: unknown
}

export function parseWindowsProcessInventory(raw: string): WindowsProcessInventoryEntry[] {
  try {
    const decoded: unknown = JSON.parse(raw)
    const rows = Array.isArray(decoded) ? decoded : [decoded]

    return rows.flatMap((value): WindowsProcessInventoryEntry[] => {
      if (!value || typeof value !== 'object') {
        return []
      }

      const row = value as RawWindowsProcess
      const pid = Number(row.ProcessId)
      const parentPid = Number(row.ParentProcessId)

      if (!Number.isInteger(pid) || pid <= 0 || !Number.isInteger(parentPid) || parentPid < 0) {
        return []
      }

      return [
        {
          pid,
          parentPid,
          executableName: typeof row.Name === 'string' ? path.win32.basename(row.Name) : 'unknown',
          executablePath: typeof row.ExecutablePath === 'string' ? row.ExecutablePath : null
        }
      ]
    })
  } catch {
    return []
  }
}

function isWithinWindowsPath(candidate: string, root: string): boolean {
  const normalizedCandidate = path.win32.resolve(candidate).toLowerCase()
  const normalizedRoot = path.win32.resolve(root).toLowerCase()
  const relative = path.win32.relative(normalizedRoot, normalizedCandidate)

  return relative !== '' && !relative.startsWith('..') && !path.win32.isAbsolute(relative)
}

export function classifyLockOwnerCandidates(
  inventory: WindowsProcessInventoryEntry[],
  venvRoot: string,
  desktopOwnedPids: ReadonlySet<number>
): WindowsLockOwnerCandidate[] {
  const desktopFamily = new Set(desktopOwnedPids)
  let changed = true

  while (changed) {
    changed = false

    for (const process of inventory) {
      if (!desktopFamily.has(process.pid) && desktopFamily.has(process.parentPid)) {
        desktopFamily.add(process.pid)
        changed = true
      }
    }
  }

  return inventory
    .filter(process => process.executablePath && isWithinWindowsPath(process.executablePath, venvRoot))
    .map((process): WindowsLockOwnerCandidate => ({
      ...process,
      ownership: desktopOwnedPids.has(process.pid)
        ? 'desktop-owned'
        : desktopFamily.has(process.pid)
          ? 'desktop-descendant'
          : 'external'
    }))
    .sort((left, right) => left.pid - right.pid)
}

const PROCESS_INVENTORY_SCRIPT =
  "$ErrorActionPreference='Stop'; @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath) | ConvertTo-Json -Compress"

export function collectWindowsLockOwnerCandidates(
  venvRoot: string,
  desktopOwnedPids: ReadonlySet<number>,
  run: (
    file: string,
    args: readonly string[],
    options: ExecFileSyncOptionsWithStringEncoding
  ) => string = execFileSync
): WindowsLockOwnerCandidate[] {
  try {
    const raw = run(
      'powershell.exe',
      ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', PROCESS_INVENTORY_SCRIPT],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 5000, windowsHide: true }
    )

    return classifyLockOwnerCandidates(parseWindowsProcessInventory(raw), venvRoot, desktopOwnedPids)
  } catch {
    return []
  }
}

export function formatWindowsLockOwnerCandidate(candidate: WindowsLockOwnerCandidate): string {
  const executable = candidate.executablePath ?? candidate.executableName

  return `PID ${candidate.pid} (parent ${candidate.parentPid}, ${candidate.ownership}): ${executable}`
}

export function buildWindowsLockTimeoutMessage(candidates: readonly WindowsLockOwnerCandidate[]): string {
  const externalCandidates = candidates.filter(candidate => candidate.ownership === 'external')
  const desktopCandidates = candidates.filter(candidate => candidate.ownership !== 'external')
  const relevantCandidates = externalCandidates.length > 0 ? externalCandidates : desktopCandidates
  const holderDetails = relevantCandidates.map(formatWindowsLockOwnerCandidate).join('; ')

  const ownershipMessage =
    externalCandidates.length > 0
      ? 'another process is holding the Hermes install open'
      : desktopCandidates.length > 0
        ? 'a Desktop-owned backend did not release the Hermes install'
        : 'the Hermes install is still locked, but Windows did not expose the holder'

  return `Update aborted: ${ownershipMessage}. ${holderDetails || 'Close other Hermes processes'} and retry.`
}
