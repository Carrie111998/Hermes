/**
 * Crews screen — crew list, create dialog (with template gallery), clone and
 * delete. Selecting a crew opens the detail screen (internal selection atom;
 * contributed routes are single-segment, so detail is state, not a route).
 */
import {
  Badge,
  Button,
  cn,
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  EmptyState,
  Input,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Textarea,
  Tip,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  $createOpen,
  $selectedCrewId,
  $templateId,
  cloneCrew,
  createCrew,
  type CreateCrewInput,
  CREWS_KEY,
  deleteCrew,
  fetchCrews,
  fetchPersonas,
  fetchTemplates,
  PERSONAS_KEY,
  TEMPLATES_KEY
} from './api'
import { CrewDetail } from './crew-detail'
import { useCrewsI18n } from './i18n'
import type { Crew, Persona } from './types'

const MAX_MEMBERS = 8

interface MemberDraft {
  persona: string
  model: string
  profileName: string
}

function MemberRow({
  index,
  draft,
  personas,
  onChange,
  onRemove,
  k
}: {
  index: number
  draft: MemberDraft
  personas: Persona[]
  onChange: (next: MemberDraft) => void
  onRemove: () => void
  k: ReturnType<typeof useCrewsI18n>
}) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_minmax(0,1fr)_auto] items-center gap-2">
      <Select onValueChange={value => onChange({ ...draft, persona: value })} value={draft.persona}>
        <SelectTrigger aria-label={`${k.persona} ${index + 1}`} className="w-full">
          <SelectValue placeholder={k.memberPlaceholder} />
        </SelectTrigger>
        <SelectContent>
          {personas.map(p => (
            <SelectItem key={p.id} value={p.id}>
              {p.emoji} {p.name} — {p.role}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <Input
        aria-label={k.model}
        onChange={e => onChange({ ...draft, model: e.target.value })}
        placeholder={k.modelPlaceholder}
        value={draft.model}
      />
      <Input
        aria-label={k.profile}
        onChange={e => onChange({ ...draft, profileName: e.target.value })}
        placeholder={k.profilePlaceholder}
        value={draft.profileName}
      />
      <Button aria-label="Remove member" onClick={onRemove} size="sm" variant="ghost">
        ×
      </Button>
    </div>
  )
}

function CreateCrewDialog({ personas, k }: { personas: Persona[]; k: ReturnType<typeof useCrewsI18n> }) {
  const qc = useQueryClient()
  const open = useValue($createOpen)
  const { data: templates } = useQuery({ queryKey: TEMPLATES_KEY, queryFn: () => fetchTemplates(), staleTime: 60_000 })

  const [name, setName] = useState('')
  const [goal, setGoal] = useState('')
  const [members, setMembers] = useState<MemberDraft[]>([{ persona: 'kai', model: '', profileName: '' }])
  const [error, setError] = useState<string | null>(null)

  const create = useMutation({
    mutationFn: (input: CreateCrewInput) => createCrew(input),
    onSuccess: data => {
      qc.invalidateQueries({ queryKey: CREWS_KEY })
      $createOpen.set(false)
      $selectedCrewId.set(data.crew.id)
      $templateId.set('')
      resetForm()
    },
    onError: (err: Error) => setError(err.message || k.createFailed)
  })

  const resetForm = () => {
    setName('')
    setGoal('')
    setMembers([{ persona: 'kai', model: '', profileName: '' }])
    setError(null)
  }

  const applyTemplate = (templateId: string) => {
    const tpl = templates?.templates.find(t => t.id === templateId)

    if (!tpl) {return}
    setName(tpl.name)
    setGoal(tpl.goal)
    setMembers(tpl.members.map(m => ({ persona: m.persona, model: '', profileName: '' })))
    $templateId.set('')
  }

  const submit = () => {
    if (!name.trim()) {return}

    const cleanMembers = members
      .filter(m => m.persona)
      .map(m => ({
        persona: m.persona,
        model: m.model.trim() || null,
        profileName: m.profileName.trim() || null
      }))

    create.mutate({ name: name.trim(), goal: goal.trim(), members: cleanMembers })
  }

  return (
    <Dialog
      onOpenChange={openState => {
        if (!openState) {
          $createOpen.set(false)
          $templateId.set('')
          resetForm()
        }
      }}
      open={open}
    >
      <DialogContent className="max-h-[85vh] w-[640px] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{k.newCrew}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          {templates && templates.templates.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs text-(--ui-text-tertiary)">{k.templates}</span>
              {templates.templates.map(t => (
                <Tip key={t.id} label={k.templateGoal(t.name)}>
                  <Badge className="cursor-pointer select-none" onClick={() => applyTemplate(t.id)} variant="outline">
                    {t.name}
                  </Badge>
                </Tip>
              ))}
            </div>
          )}

          <div className="flex flex-col gap-1">
            <label className="text-xs text-(--ui-text-secondary)" htmlFor="crew-name">
              {k.crewName}
            </label>
            <Input id="crew-name" onChange={e => setName(e.target.value)} placeholder="Crew name" value={name} />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-xs text-(--ui-text-secondary)" htmlFor="crew-goal">
              {k.goal}
            </label>
            <Textarea
              id="crew-goal"
              onChange={e => setGoal(e.target.value)}
              placeholder={k.goalPlaceholder}
              rows={2}
              value={goal}
            />
          </div>

          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="text-xs text-(--ui-text-secondary)">{k.membersCount(members.length)}</span>
              <Button
                disabled={members.length >= MAX_MEMBERS}
                onClick={() => setMembers([...members, { persona: '', model: '', profileName: '' }])}
                size="sm"
                variant="outline"
              >
                {k.addMember}
              </Button>
            </div>
            {members.length === 0 && <p className="text-xs text-(--ui-text-tertiary)">{k.noMembersHint}</p>}
            {members.map((m, i) => (
              <MemberRow
                draft={m}
                index={i}
                k={k}
                key={i}
                onChange={next => setMembers(members.map((mm, j) => (j === i ? next : mm)))}
                onRemove={() => setMembers(members.filter((_, j) => j !== i))}
                personas={personas}
              />
            ))}
          </div>

          {error && <p className="text-xs text-(--ui-danger,#f87171)">{error}</p>}
        </div>

        <DialogFooter>
          <Button
            onClick={() => {
              $createOpen.set(false)
              $templateId.set('')
              resetForm()
            }}
            variant="ghost"
          >
            {k.cancel}
          </Button>
          <Button disabled={!name.trim() || members.length === 0 || create.isPending} onClick={submit}>
            {create.isPending ? '…' : k.create}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function CrewCard({
  crew,
  k,
  onOpen,
  onClone,
  onDelete
}: {
  crew: Crew
  k: ReturnType<typeof useCrewsI18n>
  onOpen: () => void
  onClone: () => void
  onDelete: () => void
}) {
  const running = crew.members.filter(m => m.status === 'running').length
  const done = crew.members.filter(m => m.status === 'done').length

  return (
    <div
      className={cn(
        'group flex cursor-pointer flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) p-3 text-left transition-colors',
        'hover:border-(--ui-stroke-primary) hover:bg-(--chrome-action-hover)'
      )}
      onClick={onOpen}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') {onOpen()}
      }}
      role="button"
      tabIndex={0}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="font-medium text-(--ui-text-primary)">{crew.name}</span>
        <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{new Date(crew.updatedAt).toLocaleString()}</span>
      </div>
      {crew.goal && <p className="line-clamp-2 text-xs text-(--ui-text-secondary)">{crew.goal}</p>}
      <div className="flex items-center gap-1.5">
        {crew.members.slice(0, 8).map(m => (
          <Tip key={m.id} label={`${m.displayName} — ${m.roleLabel}`}>
            <span className={cn('text-sm leading-none', m.color)}>{m.displayName.charAt(0)}</span>
          </Tip>
        ))}
        <span className="ml-auto text-[0.6875rem] text-(--ui-text-tertiary)">{k.members(crew.members.length)}</span>
      </div>
      <div className="flex items-center gap-1.5">
        {running > 0 && (
          <Badge variant="outline">
            {running} {k.running}
          </Badge>
        )}
        {done > 0 && (
          <Badge variant="outline">
            {done} {k.done}
          </Badge>
        )}
        <span className="ml-auto opacity-0 transition-opacity group-hover:opacity-100">
          <DropdownMenu>
            <DropdownMenuTrigger asChild onClick={e => e.stopPropagation()}>
              <Button
                className="cursor-pointer px-1 text-(--ui-text-tertiary) hover:text-(--ui-text-primary)"
                size="sm"
                type="button"
                variant="ghost"
              >
                ⋯
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" onClick={e => e.stopPropagation()}>
              <DropdownMenuItem onSelect={onClone}>{k.clone}</DropdownMenuItem>
              <DropdownMenuItem onSelect={onDelete}>{k.delete}</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </span>
      </div>
    </div>
  )
}

export function CrewsScreen() {
  const k = useCrewsI18n()
  const qc = useQueryClient()
  const createOpen = useValue($createOpen)
  const selectedCrewId = useValue($selectedCrewId)

  const { data, isLoading, isError } = useQuery({
    queryKey: CREWS_KEY,
    queryFn: () => fetchCrews(),
    refetchInterval: 30_000
  })

  const { data: personas } = useQuery({ queryKey: PERSONAS_KEY, queryFn: () => fetchPersonas(), staleTime: 60_000 })

  const clone = useMutation({
    mutationFn: (crewId: string) => cloneCrew(crewId),
    onSuccess: data => {
      qc.invalidateQueries({ queryKey: CREWS_KEY })
      $selectedCrewId.set(data.crew.id)
    }
  })

  const remove = useMutation({
    mutationFn: (crewId: string) => deleteCrew(crewId),
    onSuccess: () => qc.invalidateQueries({ queryKey: CREWS_KEY })
  })

  const crews = data?.crews ?? []

  if (selectedCrewId) {
    return <CrewDetail crewId={selectedCrewId} />
  }

  if (isLoading) {
    return <div className="flex h-full items-center justify-center text-sm text-(--ui-text-tertiary)">…</div>
  }

  if (isError) {
    return <EmptyState description={k.notInstalledBody} title={k.notInstalled} />
  }

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-(--ui-text-primary)">{k.title}</h1>
        <Button onClick={() => $createOpen.set(true)}>{k.newCrew}</Button>
      </div>

      {crews.length === 0 ? (
        <div className="flex flex-1 items-center justify-center">
          <EmptyState description={k.emptyBody} title={k.empty} />
        </div>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3">
          {crews.map(crew => (
            <CrewCard
              crew={crew}
              k={k}
              key={crew.id}
              onClone={() => clone.mutate(crew.id)}
              onDelete={() => {
                if (window.confirm(k.deleteConfirm)) {remove.mutate(crew.id)}
              }}
              onOpen={() => $selectedCrewId.set(crew.id)}
            />
          ))}
        </div>
      )}

      {createOpen && <CreateCrewDialog k={k} personas={personas?.personas ?? []} />}
    </div>
  )
}
