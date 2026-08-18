import { useId, useMemo, useState } from 'react'

import { Input } from '@/components/ui/input'
import { ChevronDown } from '@/lib/icons'
import { cn } from '@/lib/utils'

const MODEL_PART_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/

export function isValidOpenRouterModelShape(value: string): boolean {
  const model = value.trim()

  if (!model || model.length > 256 || model.includes('\\') || model.split('/').length !== 2) {
    return false
  }

  const [author, slug] = model.split('/', 2)

  return MODEL_PART_RE.test(author) && MODEL_PART_RE.test(slug)
}

export function OpenRouterModelInput({
  className,
  hint,
  label,
  onChange,
  options,
  value
}: {
  className?: string
  hint: string
  label: string
  onChange: (value: string) => void
  options: readonly string[]
  value: string
}) {
  const [open, setOpen] = useState(false)
  // Filtering must only kick in once the user actually EDITS the field — a
  // dialog can pre-fill a complete model ID, and filtering the option list
  // by that untouched value from the first render leaves only a one-row
  // list containing what's already in the box, with no visible affordance
  // that the field is even searchable (issue C). Focus alone shows the FULL
  // list; typing narrows it.
  const [edited, setEdited] = useState(false)
  const listboxId = useId()
  const query = edited ? value.trim().toLowerCase() : ''

  const suggestions = useMemo(
    () =>
      options
        .filter(option => !query || option.toLowerCase().includes(query))
        .sort((left, right) => {
          const leftPrefix = left.toLowerCase().startsWith(query) ? 0 : 1
          const rightPrefix = right.toLowerCase().startsWith(query) ? 0 : 1

          return leftPrefix - rightPrefix || left.localeCompare(right)
        })
        .slice(0, 50),
    [options, query]
  )

  const invalid = !!value.trim() && !isValidOpenRouterModelShape(value)

  return (
    <div className={cn('relative min-w-60 flex-1', className)}>
      <Input
        aria-autocomplete="list"
        aria-controls={listboxId}
        aria-expanded={open && suggestions.length > 0}
        aria-label={label}
        className="pr-7"
        onBlur={() => window.setTimeout(() => setOpen(false), 0)}
        onChange={event => {
          setEdited(true)
          onChange(event.target.value)
          setOpen(true)
        }}
        onFocus={() => setOpen(true)}
        role="combobox"
        spellCheck={false}
        value={value}
      />
      {/* Visible searchable affordance — a plain-looking pre-filled text
       *  field otherwise gives no cue that focusing it opens a search list. */}
      <ChevronDown
        aria-hidden="true"
        className="pointer-events-none absolute top-1/2 right-2 size-3.5 -translate-y-1/2 text-muted-foreground/70"
        data-search-affordance=""
      />
      {open && suggestions.length > 0 && (
        <div
          className="absolute z-(--z-popover) mt-1 max-h-56 w-full overflow-y-auto rounded-md border border-border bg-popover p-1 shadow-md"
          id={listboxId}
          role="listbox"
        >
          {suggestions.map(option => (
            <button
              className="block w-full rounded-sm px-2 py-1.5 text-left font-mono text-xs hover:bg-accent"
              key={option}
              onClick={() => {
                setEdited(true)
                onChange(option)
                setOpen(false)
              }}
              onMouseDown={event => event.preventDefault()}
              role="option"
              type="button"
            >
              {option}
            </button>
          ))}
        </div>
      )}
      {invalid && <p className="mt-1 text-[0.66rem] leading-4 text-muted-foreground">{hint}</p>}
    </div>
  )
}
