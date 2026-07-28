import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { $cronJobs } from '@/store/cron'
import { $todosBySession } from '@/store/todos'

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const NOTES_KEY = 'hermes-calendar-notes'

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

function dateKey(y: number, m: number, d: number): string {
  return `${y}-${pad(m + 1)}-${pad(d)}`
}

function todayKey(): string {
  const d = new Date()
  return dateKey(d.getFullYear(), d.getMonth(), d.getDate())
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

interface CalendarNote {
  text: string
  updated: number
}

function loadNotes(): Record<string, CalendarNote[]> {
  try {
    return JSON.parse(localStorage.getItem(NOTES_KEY) || '{}')
  } catch {
    return {}
  }
}

function saveNotes(notes: Record<string, CalendarNote[]>) {
  localStorage.setItem(NOTES_KEY, JSON.stringify(notes))
}

interface CalendarViewProps {
  setStatusbarItemGroup?: (groups: unknown) => void
}

export function CalendarView(_props: CalendarViewProps) {
  const { t } = useI18n()
  const navigate = useNavigate()
  const cronJobs = useStore($cronJobs)
  const todosBySession = useStore($todosBySession)

  const today = useMemo(() => new Date(), [])
  const [viewYear, setViewYear] = useState(today.getFullYear())
  const [viewMonth, setViewMonth] = useState(today.getMonth())
  const [selectedDate, setSelectedDate] = useState(dateKey(today.getFullYear(), today.getMonth(), today.getDate()))
  const [notes, setNotes] = useState<Record<string, CalendarNote[]>>(loadNotes)
  const [noteDraft, setNoteDraft] = useState('')

  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate()
  const firstDayOfWeek = new Date(viewYear, viewMonth, 1).getDay()

  const prevMonth = useCallback(() => {
    if (viewMonth === 0) {
      setViewYear(y => y - 1)
      setViewMonth(11)
    } else {
      setViewMonth(m => m - 1)
    }
  }, [viewMonth])

  const nextMonth = useCallback(() => {
    if (viewMonth === 11) {
      setViewYear(y => y + 1)
      setViewMonth(0)
    } else {
      setViewMonth(m => m + 1)
    }
  }, [viewMonth])

  const goToday = useCallback(() => {
    setViewYear(today.getFullYear())
    setViewMonth(today.getMonth())
    setSelectedDate(dateKey(today.getFullYear(), today.getMonth(), today.getDate()))
  }, [today])

  // Check if a cron job's display schedule mentions this date, or next_run_at matches
  const dateHasCron = useCallback(
    (_key: string) => cronJobs.length > 0,
    [cronJobs]
  )

  const selectedCrons = useMemo(
    () => cronJobs.filter(() => true),
    [cronJobs]
  )

  const selectedNotes = notes[selectedDate] || []

  const addNote = () => {
    if (!noteDraft.trim()) return
    const updated = {
      ...notes,
      [selectedDate]: [...(notes[selectedDate] || []), { text: noteDraft.trim(), updated: Date.now() }]
    }
    setNotes(updated)
    saveNotes(updated)
    setNoteDraft('')
  }

  const deleteNote = (idx: number) => {
    const list = [...(notes[selectedDate] || [])]
    list.splice(idx, 1)
    const updated = { ...notes }
    if (list.length) {
      updated[selectedDate] = list
    } else {
      delete updated[selectedDate]
    }
    setNotes(updated)
    saveNotes(updated)
  }

  const close = useCallback(() => {
    navigate(-1)
  }, [navigate])

  const calendarDays = useMemo(() => {
    const cells: Array<{ day: number; key: string; isToday: boolean; isSelected: boolean }> = []
    for (let d = 1; d <= daysInMonth; d++) {
      const key = dateKey(viewYear, viewMonth, d)
      const dt = new Date(viewYear, viewMonth, d)
      cells.push({
        day: d,
        key,
        isToday: sameDay(dt, today),
        isSelected: key === selectedDate
      })
    }
    return cells
  }, [viewYear, viewMonth, daysInMonth, selectedDate, today])

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="flex shrink-0 items-center justify-between border-b border-(--ui-stroke-secondary) px-6 py-3">
        <h1 className="text-base font-medium text-foreground">Calendar</h1>
        <Button
          variant="ghost"
          size="icon-xs"
          onClick={close}
          aria-label="Close calendar"
          className="text-(--ui-text-tertiary) hover:text-foreground"
        >
          <Codicon name="close" size="1rem" />
        </Button>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Calendar grid */}
        <div className="flex min-w-0 flex-1 flex-col p-6 pt-4">
          {/* Month navigation */}
          <div className="flex shrink-0 items-center justify-between mb-4">
            <Button variant="ghost" size="icon-xs" onClick={prevMonth} aria-label="Previous month">
              <Codicon name="chevron-left" size="1rem" />
            </Button>
            <span className="text-sm font-medium text-foreground">
              {MONTHS[viewMonth]} {viewYear}
            </span>
            <div className="flex items-center gap-1">
              <Button variant="ghost" size="icon-xs" onClick={goToday} className="text-xs text-(--ui-text-tertiary)" aria-label="Today">
                Today
              </Button>
              <Button variant="ghost" size="icon-xs" onClick={nextMonth} aria-label="Next month">
                <Codicon name="chevron-right" size="1rem" />
              </Button>
            </div>
          </div>

          {/* Weekday headers */}
          <div className="mb-1 grid grid-cols-7 gap-px">
            {WEEKDAYS.map(day => (
              <div
                key={day}
                className="py-1 text-center text-[0.6875rem] font-medium uppercase tracking-wider text-(--ui-text-tertiary)"
              >
                {day}
              </div>
            ))}
          </div>

          {/* Calendar grid */}
          <div className="grid grid-cols-7 gap-px flex-1 auto-rows-fr">
            {Array.from({ length: firstDayOfWeek }).map((_, i) => (
              <div key={`empty-${i}`} className="min-h-0" />
            ))}
            {calendarDays.map(cell => (
              <button
                key={cell.key}
                onClick={() => setSelectedDate(cell.key)}
                className={cn(
                  'group relative flex flex-col items-center justify-start rounded-md px-1 py-1 text-xs transition-colors',
                  'hover:bg-(--ui-control-hover-background)',
                  cell.isSelected && 'bg-(--ui-control-active-background) ring-1 ring-inset ring-(--ui-stroke-tertiary)',
                )}
              >
                <span
                  className={cn(
                    'flex h-6 w-6 items-center justify-center rounded-full text-xs',
                    cell.isToday && 'bg-(--ui-accent-background) font-semibold text-(--ui-accent-foreground)',
                    !cell.isToday && 'text-foreground'
                  )}
                >
                  {cell.day}
                </span>
                {/* Dot indicators for cron/notes */}
                <div className="mt-0.5 flex items-center gap-0.5">
                  {dateHasCron(cell.key) && (
                    <span className="h-1 w-1 rounded-full bg-(--ui-accent-background)" />
                  )}
                  {notes[cell.key]?.length > 0 && (
                    <span className="h-1 w-1 rounded-full bg-yellow-500" />
                  )}
                </div>
              </button>
            ))}
          </div>
        </div>

        {/* Detail panel */}
        <div className="flex w-72 shrink-0 flex-col border-l border-(--ui-stroke-secondary) p-4">
          <h2 className="mb-3 text-sm font-medium text-foreground">{selectedDate}</h2>

          {/* Cron jobs section */}
          <div className="mb-4">
            <h3 className="mb-1.5 text-[0.6875rem] font-semibold uppercase tracking-wider text-(--ui-text-tertiary)">
              Cron Jobs
            </h3>
            {selectedCrons.length === 0 ? (
              <p className="text-xs text-(--ui-text-tertiary)">No cron jobs</p>
            ) : (
              <ul className="space-y-1 max-h-24 overflow-y-auto">
                {selectedCrons.map(job => (
                  <li key={job.id} className="flex items-center gap-1.5 text-xs text-foreground">
                    <Codicon name="clock" size="0.75rem" className="shrink-0 text-(--ui-text-tertiary)" />
                    <span className="truncate">{job.name || job.id}</span>
                    {job.schedule?.display && (
                      <span className="shrink-0 text-(--ui-text-tertiary)">{job.schedule.display}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* Todos section */}
          <div className="mb-4">
            <h3 className="mb-1.5 text-[0.6875rem] font-semibold uppercase tracking-wider text-(--ui-text-tertiary)">
              Todos
            </h3>
            {Object.keys(todosBySession).length === 0 ? (
              <p className="text-xs text-(--ui-text-tertiary)">No active todos</p>
            ) : (
              <ul className="space-y-1 max-h-24 overflow-y-auto">
                {Object.entries(todosBySession).flatMap(([sid, todos]) =>
                  todos
                    .filter(t => t.status === 'pending' || t.status === 'in_progress')
                    .map(todo => (
                      <li key={`${sid}-${todo.content}`} className="flex items-center gap-1.5 text-xs text-foreground">
                        <Codicon name={todo.status === 'in_progress' ? 'tools' : 'circle-outline'} size="0.75rem" className="shrink-0 text-(--ui-text-tertiary)" />
                        <span className="truncate">{todo.content}</span>
                      </li>
                    ))
                )}
              </ul>
            )}
          </div>

          {/* Notes section */}
          <div className="flex min-h-0 flex-1 flex-col">
            <h3 className="mb-1.5 text-[0.6875rem] font-semibold uppercase tracking-wider text-(--ui-text-tertiary)">
              Notes
            </h3>
            <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto">
              {selectedNotes.length === 0 && (
                <p className="text-xs text-(--ui-text-tertiary)">No notes for this date</p>
              )}
              {selectedNotes.map((note, idx) => (
                <div
                  key={idx}
                  className="group relative rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-control-active-background) px-2.5 py-1.5 pr-7"
                >
                  <p className="text-xs text-foreground">{note.text}</p>
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    onClick={() => deleteNote(idx)}
                    className="absolute right-0.5 top-0.5 hidden text-(--ui-text-tertiary) hover:text-destructive group-hover:flex"
                    aria-label="Delete note"
                  >
                    <Codicon name="close" size="0.625rem" />
                  </Button>
                </div>
              ))}
            </div>

            {/* Add note input */}
            <div className="mt-2 flex gap-1.5">
              <input
                type="text"
                value={noteDraft}
                onChange={e => setNoteDraft(e.target.value)}
                onKeyDown={e => {
                  if (e.key === 'Enter') addNote()
                }}
                placeholder="Add a note..."
                className="min-w-0 flex-1 rounded-md border border-(--ui-stroke-tertiary) bg-transparent px-2 py-1 text-xs text-foreground placeholder-(--ui-text-tertiary) outline-none focus:border-(--ui-accent-background)"
              />
              <Button
                variant="ghost"
                size="icon-xs"
                onClick={addNote}
                disabled={!noteDraft.trim()}
                aria-label="Add note"
              >
                <Codicon name="add" size="0.75rem" />
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
