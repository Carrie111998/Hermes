import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { requestComposerSubmit } from '@/app/chat/composer/focus'
import { $newChatProfile, $profiles } from '@/store/profile'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { cn } from '@/lib/utils'

// ── Data model ──────────────────────────────────────────────────────────

interface TodoTask {
  id: string
  text: string
}

interface ColumnDef {
  id: string
  title: string
  /** Profile key for execution columns (col1–col4). null for planned column. */
  profileKey: string | null
}

interface TodoBoard {
  columns: ColumnDef[]
  tasks: Record<string, TodoTask[]>
}

const STORAGE_KEY = 'hermes-todo-board'

const DEFAULT_BOARD: TodoBoard = {
  columns: [
    { id: 'planned', title: 'Planned', profileKey: null },
    { id: 'col1', title: 'Executor 1', profileKey: 'dimitri' },
    { id: 'col2', title: 'Executor 2', profileKey: 'dimitri-scout' },
    { id: 'col3', title: 'Executor 3', profileKey: 'dimitri-coder' },
    { id: 'col4', title: 'Executor 4', profileKey: 'dimitri-cella' }
  ],
  tasks: { planned: [], col1: [], col2: [], col3: [], col4: [] }
}

let _taskCounter = Date.now()
function genId(): string {
  return `t${++_taskCounter}`
}

// ── Storage helpers ─────────────────────────────────────────────────────

function loadBoard(): TodoBoard {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) return JSON.parse(raw) as TodoBoard
  } catch { /* ignore */ }
  return structuredClone(DEFAULT_BOARD)
}

function saveBoard(board: TodoBoard): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(board))
}

// ── Component ───────────────────────────────────────────────────────────

const CODICON_NAMES: Record<string, string> = {
  dimitri: 'robot',
  'dimitri-scout': 'eye',
  'dimitri-coder': 'zap',
  'dimitri-cella': 'comment-discussion',
  default: 'symbol-misc'
}

function profileIcon(key: string): string {
  return CODICON_NAMES[key.toLowerCase()] ?? CODICON_NAMES.default
}

export function TodoView() {
  const navigate = useNavigate()
  const [board, setBoard] = useState<TodoBoard>(loadBoard)
  const [newText, setNewText] = useState('')
  const [dragId, setDragId] = useState<string | null>(null)
  const [dragFromCol, setDragFromCol] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const profiles = useStore($profiles)

  // Persist on every change
  useEffect(() => {
    saveBoard(board)
  }, [board])

  const addTask = useCallback(() => {
    const text = newText.trim()
    if (!text) return
    setBoard(prev => {
      const next = structuredClone(prev)
      next.tasks.planned.push({ id: genId(), text })
      return next
    })
    setNewText('')
    inputRef.current?.focus()
  }, [newText])

  const deleteTask = useCallback((colId: string, taskId: string) => {
    setBoard(prev => {
      const next = structuredClone(prev)
      next.tasks[colId] = next.tasks[colId].filter(t => t.id !== taskId)
      return next
    })
  }, [])

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter') {
        e.preventDefault()
        addTask()
      }
    },
    [addTask]
  )

  // ── Drag & drop ──────────────────────────────────────────────────

  const handleDragStart = useCallback(
    (e: React.DragEvent, colId: string, taskId: string) => {
      setDragId(taskId)
      setDragFromCol(colId)
      e.dataTransfer.effectAllowed = 'move'
      e.dataTransfer.setData('text/plain', taskId)
    },
    []
  )

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
  }, [])

  const handleDrop = useCallback(
    (e: React.DragEvent, targetColId: string) => {
      e.preventDefault()
      if (!dragId || !dragFromCol || dragFromCol === targetColId) {
        setDragId(null)
        setDragFromCol(null)
        return
      }

      setBoard(prev => {
        const next = structuredClone(prev)
        const task = next.tasks[dragFromCol].find(t => t.id === dragId)
        if (!task) return prev
        next.tasks[dragFromCol] = next.tasks[dragFromCol].filter(t => t.id !== dragId)
        next.tasks[targetColId].push(task)
        return next
      })

      setDragId(null)
      setDragFromCol(null)
    },
    [dragId, dragFromCol]
  )

  const handleDragEnd = useCallback(() => {
    setDragId(null)
    setDragFromCol(null)
  }, [])

  // ── Profile selection ────────────────────────────────────────────

  const knownProfiles = ['dimitri', 'dimitri-scout', 'dimitri-coder', 'dimitri-cella', 'default']

  // Merge live profiles from store with known defaults. No empty options.
  const allProfileKeys = useCallback(() => {
    const names = new Set(profiles.map(p => p.name.toLowerCase()))
    knownProfiles.forEach(n => names.add(n))
    const ordered = knownProfiles.filter(n => names.has(n))
    const extras = [...names].filter(n => !ordered.includes(n))
    return [...ordered, ...extras]
  }, [profiles])

  const setProfile = useCallback((colId: string, profileKey: string) => {
    setBoard(prev => {
      const next = structuredClone(prev)
      const col = next.columns.find(c => c.id === colId)
      if (col) col.profileKey = profileKey
      return next
    })
  }, [])

  // ── Execute task ─────────────────────────────────────────────────

  const runTask = useCallback((taskText: string, profileKey: string) => {
    $newChatProfile.set(profileKey)
    navigate('/')
    setTimeout(() => requestComposerSubmit(taskText, { target: 'main' }), 300)
  }, [navigate])

  // ── Render ───────────────────────────────────────────────────────

  return (
    <div className="flex h-full flex-col overflow-hidden bg-(--ui-editor-surface-background)">
      {/* Header */}
      <div className="flex shrink-0 items-center gap-2 border-b border-(--ui-stroke-tertiary) px-4 py-3">
        <Codicon name="checklist" className="text-(--ui-text-secondary)" size="1.25rem" />
        <h1 className="text-sm font-semibold text-foreground">Todo Board</h1>
      </div>

      {/* Board */}
      <div className="flex flex-1 gap-3 overflow-x-auto p-4">
        {board.columns.map(col => {
          const isPlanned = col.id === 'planned'
          const tasks = board.tasks[col.id] ?? []

          return (
            <div
              key={col.id}
              className={cn(
                'flex shrink-0 flex-col rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-sidebar-surface-background)',
                isPlanned ? 'w-72' : 'w-56'
              )}
              onDragOver={handleDragOver}
              onDrop={e => handleDrop(e, col.id)}
            >
              {/* Column header */}
              <div className="flex items-center gap-1.5 border-b border-(--ui-stroke-tertiary) px-3 py-2">
                {!isPlanned ? (
                  <Codicon name={profileIcon(col.profileKey ?? 'default')} className="text-(--ui-text-tertiary)" size="0.875rem" />
                ) : (
                  <Codicon name="list-unordered" className="text-(--ui-text-tertiary)" size="0.875rem" />
                )}
                <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
                  {isPlanned ? col.title : col.title}
                </span>
                {!isPlanned && (
                  <ProfileSelect
                    value={col.profileKey ?? ''}
                    options={allProfileKeys()}
                    onChange={key => setProfile(col.id, key)}
                  />
                )}
                <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{tasks.length}</span>
              </div>

              {/* Task list */}
              <div className="flex flex-1 flex-col gap-1 overflow-y-auto p-2">
                {tasks.map(task => (
                  <div
                    key={task.id}
                    draggable
                    onDragStart={e => handleDragStart(e, col.id, task.id)}
                    onDragEnd={handleDragEnd}
                    className={cn(
                      'group flex cursor-grab items-start gap-1 rounded-md border border-transparent px-2 py-1.5 text-xs leading-snug text-foreground transition-colors',
                      'hover:border-(--ui-stroke-tertiary) hover:bg-(--ui-control-hover-background)',
                      dragId === task.id && 'opacity-40'
                    )}
                  >
                    <span className="min-w-0 flex-1 break-words">{task.text}</span>

                    {/* Run button — always visible on exec columns */}
                    {!isPlanned && (
                      <Button
                        size="icon-xs"
                        variant="ghost"
                        className="shrink-0 text-(--ui-text-secondary)"
                        onClick={() => runTask(task.text, col.profileKey ?? 'default')}
                        title="Run task"
                      >
                        <Codicon name="play" size="0.75rem" />
                      </Button>
                    )}

                    <Button
                      size="icon-xs"
                      variant="ghost"
                      className="shrink-0 opacity-0 transition-opacity group-hover:opacity-100"
                      onClick={() => deleteTask(col.id, task.id)}
                      title="Delete task"
                    >
                      <Codicon name="trash" size="0.75rem" />
                    </Button>
                  </div>
                ))}

                {/* Empty state */}
                {tasks.length === 0 && (
                  <div className="flex min-h-16 items-center justify-center text-[0.6875rem] text-(--ui-text-tertiary)">
                    {isPlanned ? 'No tasks yet' : 'Drop a task here'}
                  </div>
                )}
              </div>

              {/* Add input (only for planned column) */}
              {isPlanned && (
                <div className="flex items-center gap-1 border-t border-(--ui-stroke-tertiary) p-2">
                  <input
                    ref={inputRef}
                    className="min-w-0 flex-1 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-sidebar-surface-background) px-2 py-1 text-xs text-foreground outline-none placeholder:text-(--ui-text-tertiary)"
                    placeholder="Add task..."
                    value={newText}
                    onChange={e => setNewText(e.target.value)}
                    onKeyDown={handleKeyDown}
                  />
                  <Button size="icon-xs" variant="secondary" onClick={addTask} title="Add task">
                    <Codicon name="add" size="0.75rem" />
                  </Button>
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Custom profile dropdown (matches dark theme, native <select> uses OS/white) ──

function ProfileSelect({
  value,
  options,
  onChange
}: {
  value: string
  options: string[]
  onChange: (key: string) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  return (
    <div ref={ref} className="relative inline-flex items-center">
      <button
        onClick={() => setOpen(!open)}
        className={cn(
          'inline-flex items-center gap-0.5 rounded border px-1.5 py-0.5 text-[0.6875rem] text-foreground',
          'border-(--ui-stroke-tertiary) bg-(--ui-sidebar-surface-background) hover:border-(--ui-stroke-secondary)'
        )}
      >
        <span className="truncate max-w-28">{value || 'select'}</span>
        <Codicon name="chevron-down" size="0.625rem" className="shrink-0 text-(--ui-text-tertiary)" />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-0.5 w-36 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated) py-1 shadow-lg">
          {options.map(key => (
            <button
              key={key}
              className={cn(
                'flex w-full items-center gap-1.5 px-2 py-1 text-left text-[0.6875rem] text-foreground hover:bg-(--ui-control-hover-background)',
                key === value && 'font-medium'
              )}
              onClick={() => {
                onChange(key)
                setOpen(false)
              }}
            >
              {key}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
