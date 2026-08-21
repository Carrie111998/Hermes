import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { useI18n } from '@/i18n'
import { ChevronLeft, MessageSquareText, Pencil, Plus } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { confirm } from '@/store/confirm'

import { createSnippetId, loadSnippets, type PromptSnippet, saveSnippets } from './prompt-snippets'

interface PromptSnippetsDialogProps {
  onInsertText: (text: string) => void
  onOpenChange: (open: boolean) => void
  open: boolean
}

interface Draft {
  description: string
  id: null | string
  label: string
  text: string
}

const emptyDraft = (): Draft => ({ id: null, label: '', description: '', text: '' })

export function PromptSnippetsDialog({ onInsertText, onOpenChange, open }: PromptSnippetsDialogProps) {
  const { t } = useI18n()
  const c = t.composer
  const [items, setItems] = useState<PromptSnippet[]>([])
  const [draft, setDraft] = useState<Draft | null>(null)

  useEffect(() => {
    if (!open) {
      return
    }

    setItems(loadSnippets(c.snippets))
    setDraft(null)
  }, [c.snippets, open])

  function persist(next: PromptSnippet[]) {
    setItems(next)
    saveSnippets(next)
  }

  function insert(snippet: PromptSnippet) {
    onInsertText(snippet.text)
    onOpenChange(false)
  }

  async function removeDraft() {
    if (!draft?.id) {
      return
    }

    const ok = await confirm({
      title: c.snippetsDeleteTitle,
      description: c.snippetsDeleteDesc(draft.label || draft.id),
      confirmLabel: t.common.delete,
      destructive: true
    })

    if (!ok) {
      return
    }

    persist(items.filter(item => item.id !== draft.id))
    setDraft(null)
  }

  function saveDraft() {
    if (!draft) {
      return
    }

    const label = draft.label.trim()
    const text = draft.text

    if (!label || !text.trim()) {
      return
    }

    const nextItem: PromptSnippet = {
      id: draft.id ?? createSnippetId(),
      label,
      description: draft.description.trim(),
      text
    }
    const index = items.findIndex(item => item.id === nextItem.id)
    const next = index >= 0 ? items.map((item, i) => (i === index ? nextItem : item)) : [...items, nextItem]

    persist(next)
    setDraft(null)
  }

  const editing = draft !== null

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className={editing ? 'max-w-lg' : 'max-w-md'}>
        <DialogHeader className="flex-row items-start gap-2 pr-10 sm:text-left">
          <div className="grid min-w-0 flex-1 gap-1">
            <DialogTitle>{editing ? (draft.id ? c.snippetsEditTitle : c.snippetsAddTitle) : c.snippetsTitle}</DialogTitle>
            <DialogDescription>{editing ? c.snippetsEditDesc : c.snippetsDesc}</DialogDescription>
          </div>
          {editing ? (
            <Button
              aria-label={t.common.back}
              className="text-(--ui-text-tertiary) hover:text-foreground"
              onClick={() => setDraft(null)}
              size="icon-xs"
              type="button"
              variant="ghost"
            >
              <ChevronLeft className="size-3.5" />
            </Button>
          ) : (
            <Button
              aria-label={c.snippetsAdd}
              className="text-(--ui-text-tertiary) hover:text-foreground"
              onClick={() => setDraft(emptyDraft())}
              size="icon-xs"
              type="button"
              variant="ghost"
            >
              <Plus className="size-3.5" />
            </Button>
          )}
        </DialogHeader>

        {editing && draft ? (
          <form
            className="grid gap-2.5"
            onSubmit={event => {
              event.preventDefault()
              saveDraft()
            }}
          >
            <label className="grid gap-1">
              <span className="text-[0.6875rem] font-medium uppercase tracking-wider text-(--ui-text-tertiary)">
                {c.snippetsName}
              </span>
              <Input
                autoFocus
                onChange={event => setDraft({ ...draft, label: event.target.value })}
                value={draft.label}
              />
            </label>
            <label className="grid gap-1">
              <span className="text-[0.6875rem] font-medium uppercase tracking-wider text-(--ui-text-tertiary)">
                {c.snippetsHint}
              </span>
              <Input
                onChange={event => setDraft({ ...draft, description: event.target.value })}
                value={draft.description}
              />
            </label>
            <label className="grid gap-1">
              <span className="text-[0.6875rem] font-medium uppercase tracking-wider text-(--ui-text-tertiary)">
                {c.snippetsTemplate}
              </span>
              <Textarea
                className="min-h-40"
                onChange={event => setDraft({ ...draft, text: event.target.value })}
                value={draft.text}
              />
            </label>
            <DialogFooter className="gap-2 sm:justify-between">
              {draft.id ? (
                <Button onClick={() => void removeDraft()} type="button" variant="ghost">
                  {t.common.delete}
                </Button>
              ) : (
                <span />
              )}
              <div className="flex gap-2">
                <Button onClick={() => setDraft(null)} type="button" variant="ghost">
                  {t.common.cancel}
                </Button>
                <Button disabled={!draft.label.trim() || !draft.text.trim()} type="submit">
                  {t.common.save}
                </Button>
              </div>
            </DialogFooter>
          </form>
        ) : (
          <ul className="grid gap-1">
            {items.map(snippet => (
              <li className="group/snippet flex items-start gap-0.5" key={snippet.id}>
                <button
                  className="flex min-w-0 flex-1 cursor-pointer items-start gap-2.5 rounded-md border border-transparent px-2.5 py-2 text-left transition-colors hover:border-(--ui-stroke-tertiary) hover:bg-(--ui-control-hover-background) focus-visible:border-(--ui-stroke-tertiary) focus-visible:bg-(--ui-control-hover-background) focus-visible:outline-none"
                  onClick={() => insert(snippet)}
                  type="button"
                >
                  <MessageSquareText className="mt-0.5 size-3.5 shrink-0 text-(--ui-text-tertiary) group-hover/snippet:text-foreground" />
                  <span className="grid min-w-0 gap-0.5">
                    <span className="text-sm font-medium text-foreground">{snippet.label}</span>
                    {snippet.description ? (
                      <span className="truncate text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">
                        {snippet.description}
                      </span>
                    ) : null}
                  </span>
                </button>
                <Button
                  aria-label={`${c.snippetsEdit}: ${snippet.label}`}
                  className={cn(
                    'mt-1.5 text-(--ui-text-tertiary) opacity-0 hover:text-foreground group-hover/snippet:opacity-100 group-focus-within/snippet:opacity-100'
                  )}
                  onClick={() => setDraft({ ...snippet })}
                  size="icon-xs"
                  type="button"
                  variant="ghost"
                >
                  <Pencil className="size-3.5" />
                </Button>
              </li>
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  )
}
