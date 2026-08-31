import { atom, computed } from 'nanostores'

type Updater<T> = T | ((current: T) => T)

interface WritableStore<T> {
  get: () => T
  set: (value: T) => void
}

const DEFAULT_CONSOLE_HEIGHT = 240
/** How many messages one tab's console keeps. A page in a render loop is
 *  unbounded; this is not. */
const MAX_LOGS = 200

export interface ConsoleEntry {
  id: number
  level: number
  line?: number
  message: string
  source?: string
}

/** Chromium's own names, as `webContents`' console-message reports them since
 *  Electron 35. `warning` is the spelling that matters — it is what a missing
 *  translation key arrives as. */
const LEVEL_NUMBER: Record<string, number> = {
  debug: 0,
  error: 3,
  info: 1,
  log: 0,
  verbose: 0,
  warn: 2,
  warning: 2
}

/**
 * One numeric convention for a level that arrives in two shapes.
 *
 * `webContents` moved to a STRING level in Electron 35; `<webview>`, which is
 * what the pane actually hosts, was never migrated and still emits an Integer
 * 0-3. The whole app — the panel's colours and labels, and the digest that
 * tells the agent how many errors a page threw — compares numbers.
 *
 * So this is insurance against a silent death rather than a live bug: if that
 * divergence is ever settled, a string level would quietly stop equalling 3,
 * every error would count as a `log`, and the agent would go back to being
 * told a broken page is fine. Nothing would throw.
 */
export function consoleLevel(level: number | string | undefined): number {
  if (typeof level === 'number') {
    return Number.isFinite(level) ? level : 0
  }

  return typeof level === 'string' ? (LEVEL_NUMBER[level.toLowerCase()] ?? 0) : 0
}

export interface ConsoleEntryInput {
  level: number
  line?: number
  message: string
  source?: string
}

function updateAtom<T>(store: WritableStore<T>, next: Updater<T>) {
  store.set(typeof next === 'function' ? (next as (current: T) => T)(store.get()) : next)
}

export function createPreviewConsoleState() {
  const $height = atom(DEFAULT_CONSOLE_HEIGHT)
  const $logs = atom<ConsoleEntry[]>([])
  const $logCount = computed($logs, logs => logs.length)
  const $open = atom(false)
  const $selectedLogIds = atom<ReadonlySet<number>>(new Set())
  let nextLogId = 0
  // Whether anything has ever been dropped off the front. `nextLogId` alone
  // cannot say: `clear` empties the log without resetting it, so a cleared
  // console would look overflowed forever.
  let overflowed = false
  // How much of this console the AGENT has been told about. It lives here
  // rather than beside the reporter because only this store knows when the log
  // restarts: both `clear` and `reset` make every later message new again, and
  // a watermark kept outside could not tell a cleared console from a quiet one
  // (the count returns to where it was). See preview-console-digest.ts.
  let reportedId = 0

  return {
    $height,
    $logCount,
    $logs,
    $open,
    $selectedLogIds,
    append(entry: ConsoleEntryInput) {
      const logs = $logs.get()

      if (logs.length >= MAX_LOGS) {
        overflowed = true
      }

      $logs.set([...logs.slice(-(MAX_LOGS - 1)), { ...entry, id: ++nextLogId }])
    },
    clear() {
      $logs.set([])
      $selectedLogIds.set(new Set())
      reportedId = 0
      overflowed = false
    },
    clearSelection() {
      if ($selectedLogIds.get().size === 0) {
        return
      }

      $selectedLogIds.set(new Set())
    },
    /** Everything logged since the agent was last told, and mark it told. */
    drainUnreported(): ConsoleEntry[] {
      const logs = $logs.get()
      const fresh = logs.filter(log => log.id > reportedId)

      reportedId = logs.length > 0 ? logs[logs.length - 1].id : reportedId

      return fresh
    },
    /** Whether messages have been dropped off the front, so the counts a caller
     *  computes over `$logs` are floors rather than totals. */
    overflowed: () => overflowed,
    reset() {
      nextLogId = 0
      $logs.set([])
      $selectedLogIds.set(new Set())
      reportedId = 0
      overflowed = false
    },
    setHeight(next: Updater<number>) {
      updateAtom($height, next)
    },
    setOpen(next: Updater<boolean>) {
      updateAtom($open, next)
    },
    toggleSelection(id: number) {
      const next = new Set($selectedLogIds.get())

      if (!next.delete(id)) {
        next.add(id)
      }

      $selectedLogIds.set(next)
    }
  }
}

export type PreviewConsoleState = ReturnType<typeof createPreviewConsoleState>
