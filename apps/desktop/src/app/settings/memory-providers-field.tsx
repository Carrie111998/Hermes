import { useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { Info, Plus, X } from '@/lib/icons'
import { prettyName } from '@/lib/text'
import { cn } from '@/lib/utils'

import { CONTROL_TEXT } from './constants'

// Normalize the raw config value (`memory.providers`: an ordered list of
// provider slugs) into editor rows. Defensive against a legacy singular
// string (`memory.provider` bled through) so the editor never crashes on odd
// data.
function normalizeRows(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(item => (typeof item === 'string' ? item : String(item ?? '')))
  }

  if (typeof value === 'string' && value.trim()) {
    return [value.trim()]
  }

  return []
}

function completeRows(rows: string[]): string[] {
  // Only complete (non-empty) values are emitted upward, so autosave never
  // persists a blank provider slot.
  return rows.filter(Boolean)
}

function rowsEqual(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index])
}

/**
 * Structured editor for the `memory.providers` config list — an ordered set of
 * external memory providers layered on top of built-in memory, tried/injected
 * in list order (list order == priority == injection order, #5688 FR-7).
 *
 * A single-value-per-row clone of `FallbackModelsField`: numbered ordered rows,
 * one `<Select>` per row, `X` remove, `+ Add` at the bottom. Order is the data,
 * so a checklist/tag cloud (which can't express priority) is deliberately NOT
 * used. Half-filled rows stay in local state and only complete values are
 * emitted upward, and a `lastEmittedRef` echo-guard ignores autosave echoing
 * `value` back so a draft row is never wiped.
 *
 * `options` are the discovery-driven provider slugs merged into the field
 * schema server-side (`_memory_provider_schema_options`), which also preserves
 * a configured-but-removed-from-disk provider so it renders (with a warning)
 * instead of silently vanishing.
 */
export function MemoryProvidersField({
  value,
  onChange,
  options
}: {
  value: unknown
  onChange: (next: string[]) => void
  options: string[]
}) {
  const { t } = useI18n()
  const c = t.settings.config
  const m = t.settings.model

  // Discovered provider slugs (drop the empty "built-in" sentinel — an empty
  // list already means built-in only, expressed by removing all rows).
  const catalog = options.filter(Boolean)

  const [rows, setRows] = useState<string[]>(() => normalizeRows(value))
  // Last complete list we emitted (or seeded). Autosave echoes the same
  // filtered list back through `value`; ignore that echo so draft rows stay.
  const lastEmittedRef = useRef(normalizeRows(value))

  // Resync on real external changes (profile switch / config reload). Skip
  // when `value` is just our own commit echoing through the parent.
  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (mirror of fallback field)
  useEffect(() => {
    const persisted = normalizeRows(value)

    if (rowsEqual(persisted, lastEmittedRef.current)) {
      return
    }

    lastEmittedRef.current = persisted
    setRows(persisted)
  }, [value])

  const commit = (next: string[]) => {
    const complete = completeRows(next)

    setRows(next)
    lastEmittedRef.current = complete
    onChange(complete)
  }

  const updateRow = (index: number, next: string) =>
    commit(rows.map((slug, i) => (i === index ? next : slug)))

  return (
    <div className="grid w-full gap-1.5">
      {rows.length === 0 && <p className="text-xs text-muted-foreground">{c.memoryProvidersEmpty}</p>}
      {rows.map((slug, index) => {
        // Duplicate guard: a slug chosen in another row is disabled here, so
        // the UI can never author a [honcho, honcho] the resolver would then
        // silently collapse — surface the constraint, don't hide it.
        const takenElsewhere = new Set(rows.filter((_, i) => i !== index).filter(Boolean))
        // Keep an out-of-catalog current value selectable so a provider removed
        // from disk renders (with a warning) instead of showing a blank box.
        const items = slug && !catalog.includes(slug) ? [slug, ...catalog] : catalog
        const notInstalled = Boolean(slug) && !catalog.includes(slug)

        return (
          <div className="flex flex-wrap items-center gap-2" key={index}>
            <span className="w-4 shrink-0 text-center font-mono text-[0.7rem] text-muted-foreground">
              {index + 1}
            </span>
            <Select onValueChange={next => updateRow(index, next)} value={slug}>
              <SelectTrigger className={cn('min-w-52 flex-1', CONTROL_TEXT)}>
                <SelectValue placeholder={c.memoryProvidersPlaceholder} />
              </SelectTrigger>
              <SelectContent>
                {items.map(option => (
                  <SelectItem disabled={takenElsewhere.has(option)} key={option} value={option}>
                    {prettyName(option)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {notInstalled ? (
              <Tip label={c.memoryProviderNotInstalled}>
                <span className="inline-flex items-center text-muted-foreground">
                  <Info className="size-3.5" />
                </span>
              </Tip>
            ) : null}
            <Button
              aria-label={t.common.remove}
              onClick={() => commit(rows.filter((_, i) => i !== index))}
              size="icon-xs"
              variant="ghost"
            >
              <X className="size-3.5" />
            </Button>
          </div>
        )
      })}
      <div>
        <Button onClick={() => commit([...rows, ''])} size="sm" variant="textStrong">
          <Plus className="size-3.5" />
          {c.memoryProvidersAdd}
        </Button>
      </div>
    </div>
  )
}
