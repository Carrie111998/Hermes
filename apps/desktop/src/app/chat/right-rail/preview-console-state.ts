import { atom, computed } from 'nanostores'

type Updater<T> = T | ((current: T) => T)

interface WritableStore<T> {
  get: () => T
  set: (value: T) => void
}

const DEFAULT_CONSOLE_HEIGHT = 240

export interface ConsoleEntry {
  id: number
  level: number
  line?: number
  message: string
  source?: string
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
      $logs.set([...$logs.get().slice(-199), { ...entry, id: ++nextLogId }])
    },
    clear() {
      $logs.set([])
      $selectedLogIds.set(new Set())
      reportedId = 0
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
    reset() {
      nextLogId = 0
      $logs.set([])
      $selectedLogIds.set(new Set())
      reportedId = 0
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
