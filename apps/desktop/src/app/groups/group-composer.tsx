import { type ChangeEvent, type KeyboardEvent, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'

interface GroupComposerProps {
  ariaLabel: string
  mentionLabel: string
  profiles: readonly string[]
  profileLabels: Readonly<Record<string, string>>
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
}

interface MentionTrigger {
  end: number
  query: string
  start: number
}

function mentionTrigger(value: string, cursor: number): MentionTrigger | null {
  const prefix = value.slice(0, cursor)
  const match = /(^|\s)@([^\s@]*)$/u.exec(prefix)

  if (!match) {return null}

  return { end: cursor, query: match[2].toLowerCase(), start: cursor - match[2].length - 1 }
}

export function GroupComposer({ ariaLabel, mentionLabel, onChange, onSubmit, profileLabels, profiles, value }: GroupComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [trigger, setTrigger] = useState<MentionTrigger | null>(null)

  const candidates = useMemo(
    () => trigger ? profiles.filter(profile =>
      profile.toLowerCase().includes(trigger.query) || (profileLabels[profile] ?? '').toLowerCase().includes(trigger.query)
    ) : [],
    [profileLabels, profiles, trigger]
  )


  const updateTrigger = (nextValue: string, cursor: number | null) => {
    setTrigger(mentionTrigger(nextValue, cursor ?? nextValue.length))
  }

  const change = (event: ChangeEvent<HTMLTextAreaElement>) => {
    onChange(event.target.value)
    updateTrigger(event.target.value, event.target.selectionStart)
  }

  const choose = (profile: string) => {
    if (!trigger) {return}

    const next = `${value.slice(0, trigger.start)}@${profile} ${value.slice(trigger.end)}`
    const cursor = trigger.start + profile.length + 2
    onChange(next)
    setTrigger(null)
    requestAnimationFrame(() => {
      textareaRef.current?.focus()
      textareaRef.current?.setSelectionRange(cursor, cursor)
    })
  }

  const keyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing) {return}

    if (event.key === 'Enter' && !event.shiftKey && candidates.length > 0) {
      event.preventDefault()
      choose(candidates[0])

      return
    }

    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      onSubmit()

      return
    }

    if (event.key === 'Escape' && trigger) {
      event.preventDefault()
      setTrigger(null)
    }
  }

  return <div className="relative min-w-0 flex-1">
    {trigger && candidates.length > 0 && <div aria-label={mentionLabel} className="absolute bottom-full left-0 z-50 mb-1 grid min-w-48 gap-0.5 rounded-md border border-border bg-popover p-1 shadow-md" role="listbox">
      {candidates.map(profile => <Button aria-label={`@${profile}${profileLabels[profile] ? ` ${profileLabels[profile]}` : ''}`} className="justify-between gap-4" key={profile} onClick={() => choose(profile)} role="option" size="sm" type="button" variant="text"><span>@{profile}</span>{profileLabels[profile] && <span className="text-muted-foreground">{profileLabels[profile]}</span>}</Button>)}
    </div>}
    <textarea
      aria-label={ariaLabel}
      autoCapitalize="off"
      autoComplete="off"
      autoCorrect="off"
      className="relative block max-h-[60dvh] min-h-10 w-full min-w-0 resize-y overflow-auto border-0 bg-transparent px-2 py-2 text-sm leading-5 text-foreground caret-foreground outline-none selection:bg-primary/25"
      onChange={change}
      onClick={event => updateTrigger(value, event.currentTarget.selectionStart)}
      onKeyDown={keyDown}
      onKeyUp={event => updateTrigger(value, event.currentTarget.selectionStart)}
      ref={textareaRef}
      spellCheck={false}
      value={value}
    />
  </div>
}