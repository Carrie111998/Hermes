/**
 * Crew detail — member grid with live status, task dispatch, the activity
 * feed, and the visual workflow builder tab.
 */
import {
  Badge,
  Button,
  cn,
  ScrollArea,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  StatusDot,
  Tabs,
  TabsList,
  TabsTrigger,
  Textarea,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  $selectedCrewId,
  crewKey,
  deleteCrew,
  dispatchTask,
  fetchCrew,
  fetchRuns,
  fetchWorkflow,
  runsKey,
  useActivityFeed,
  workflowKey
} from './api'
import { useCrewsI18n } from './i18n'
import type { Crew, CrewEvent, CrewMember } from './types'
import { WorkflowBuilder } from './workflow-builder'

function memberTone(member: CrewMember): 'good' | 'muted' | 'warn' | 'bad' {
  switch (member.status) {
    case 'running':
      return 'warn'

    case 'done':
      return 'good'

    case 'error':
      return 'bad'

    default:
      return 'muted'
  }
}

function MemberCard({ member, k }: { member: CrewMember; k: ReturnType<typeof useCrewsI18n> }) {
  return (
    <div
      className={cn(
        'flex flex-col gap-1.5 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) p-3',
        member.status === 'running' && 'border-(--ui-accent)'
      )}
    >
      <div className="flex items-center gap-2">
        <StatusDot className={cn(member.status === 'running' && 'animate-pulse')} tone={memberTone(member)} />
        <span className="font-medium text-(--ui-text-primary)">{member.displayName}</span>
      </div>
      <span className="text-xs text-(--ui-text-secondary)">{member.roleLabel}</span>
      <div className="flex flex-wrap gap-1">
        {member.model && <Badge variant="outline">{member.model}</Badge>}
        <Badge variant="outline">{member.profileName ?? member.persona}</Badge>
        <Badge variant="outline">{member.status}</Badge>
      </div>
      {member.lastActivity && (
        <p className="line-clamp-3 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">{member.lastActivity}</p>
      )}
    </div>
  )
}

function formatEvent(
  ev: CrewEvent,
  k: ReturnType<typeof useCrewsI18n>
): { text: string; tone: 'good' | 'muted' | 'warn' | 'bad' } {
  switch (ev.type) {
    case 'member_status':
      return {
        text: `${ev.memberId.slice(0, 8)} → ${ev.status}${ev.detail ? ` — ${ev.detail}` : ''}`,
        tone: ev.status === 'error' ? 'bad' : ev.status === 'running' ? 'warn' : 'good'
      }

    case 'task_status':
      return {
        text: `task ${ev.taskId.slice(0, 8)} → ${ev.status}${ev.detail ? ` — ${ev.detail}` : ''}`,
        tone: ev.status === 'error' ? 'bad' : ev.status === 'running' ? 'warn' : 'good'
      }

    case 'worker_end':
      return {
        text: `worker ${(ev.memberId ?? ev.taskId ?? '').slice(0, 8)} finished ${ev.status}${ev.detail ?? ''}`,
        tone: ev.status === 'error' ? 'bad' : 'good'
      }

    case 'activity':
      return { text: ev.text.slice(0, 160), tone: 'muted' }

    case 'run_started':
      return { text: `workflow run ${ev.runId.slice(0, 8)} started`, tone: 'warn' }

    case 'run_end':
      return {
        text: `workflow run ${ev.runId.slice(0, 8)} → ${ev.status}`,
        tone: ev.status === 'error' ? 'bad' : 'good'
      }

    case 'crew_updated':
      return { text: k.updated, tone: 'muted' }

    case 'crew_deleted':
      return { text: 'crew deleted', tone: 'bad' }

    default:
      return { text: JSON.stringify(ev).slice(0, 120), tone: 'muted' }
  }
}

function DispatchPanel({ crew, k }: { crew: Crew; k: ReturnType<typeof useCrewsI18n> }) {
  const qc = useQueryClient()
  const [task, setTask] = useState('')
  const [target, setTarget] = useState('all')

  const dispatch = useMutation({
    mutationFn: ({ taskText, targetId }: { taskText: string; targetId: string }) =>
      dispatchTask(crew.id, taskText, targetId === 'all' ? 'all' : targetId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: crewKey(crew.id) })
      setTask('')
    }
  })

  const submit = () => {
    const taskText = task.trim()

    if (!taskText) {return}
    dispatch.mutate({ taskText, targetId: target })
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-(--ui-text-primary)">{k.dispatch}</span>
        <Select onValueChange={setTarget} value={target}>
          <SelectTrigger aria-label={k.dispatchTo} className="w-[180px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">{k.allMembers}</SelectItem>
            {crew.members.map(m => (
              <SelectItem key={m.id} value={m.id}>
                {m.displayName}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <Textarea onChange={e => setTask(e.target.value)} placeholder={k.dispatchPlaceholder} rows={2} value={task} />
      <div className="flex justify-end">
        <Button disabled={!task.trim() || dispatch.isPending} onClick={submit}>
          {dispatch.isPending ? '…' : k.dispatch}
        </Button>
      </div>
    </div>
  )
}

function ActivityFeed({ crewId, k }: { crewId: string; k: ReturnType<typeof useCrewsI18n> }) {
  const feed = useActivityFeed()
  const scoped = feed.filter(ev => 'crewId' in ev && ev.crewId === crewId)

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary) p-3">
      <span className="text-sm font-medium text-(--ui-text-primary)">{k.activity}</span>
      {scoped.length === 0 ? (
        <p className="text-xs text-(--ui-text-tertiary)">{k.noActivity}</p>
      ) : (
        <ScrollArea className="min-h-0 flex-1">
          <ul className="flex flex-col gap-1">
            {scoped.map((ev, i) => {
              const { text, tone } = formatEvent(ev, k)

              return (
                <li className="flex items-start gap-1.5 text-xs leading-snug" key={i}>
                  <StatusDot tone={tone} />
                  <span className="text-(--ui-text-secondary)">
                    {new Date(ev.ts).toLocaleTimeString()} · {text}
                  </span>
                </li>
              )
            })}
          </ul>
        </ScrollArea>
      )}
    </div>
  )
}

export function CrewDetail({ crewId }: { crewId: string }) {
  const k = useCrewsI18n()
  const qc = useQueryClient()
  const [tab, setTab] = useState('overview')

  const { data, isLoading, isError } = useQuery({
    queryKey: crewKey(crewId),
    queryFn: () => fetchCrew(crewId),
    refetchInterval: 15_000
  })

  // Keep workflow + runs warm so the canvas tab opens with data.
  useQuery({ queryKey: workflowKey(crewId), queryFn: () => fetchWorkflow(crewId) })
  useQuery({ queryKey: runsKey(crewId), queryFn: () => fetchRuns(crewId), refetchInterval: 15_000 })

  const remove = useMutation({
    mutationFn: () => deleteCrew(crewId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['crews', 'list'] })
      $selectedCrewId.set('')
    }
  })

  if (isLoading) {
    return <div className="flex h-full items-center justify-center text-sm text-(--ui-text-tertiary)">…</div>
  }

  if (isError || !data?.crew) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-(--ui-text-tertiary)">{k.notInstalled}</div>
    )
  }

  const crew = data.crew

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-center gap-2">
        <Button onClick={() => $selectedCrewId.set('')} size="sm" variant="ghost">
          ← {k.back}
        </Button>
        <h1 className="text-lg font-semibold text-(--ui-text-primary)">{crew.name}</h1>
        <Badge variant="outline">{crew.status}</Badge>
        <span className="ml-auto flex gap-2">
          <Button
            onClick={() => {
              if (window.confirm(k.deleteConfirm)) {remove.mutate()}
            }}
            size="sm"
            variant="ghost"
          >
            {k.delete}
          </Button>
        </span>
      </div>

      {crew.goal && <p className="text-sm text-(--ui-text-secondary)">{crew.goal}</p>}

      <Tabs onValueChange={setTab} value={tab}>
        <TabsList>
          <TabsTrigger value="overview">{k.overview}</TabsTrigger>
          <TabsTrigger value="workflow">{k.workflow}</TabsTrigger>
        </TabsList>
      </Tabs>

      {tab === 'overview' && (
        <div className="flex min-h-0 flex-1 flex-col gap-3 lg:flex-row">
          <div className="flex min-h-0 flex-1 flex-col gap-3">
            <div className="grid grid-cols-[repeat(auto-fill,minmax(220px,1fr))] gap-2">
              {crew.members.map(m => (
                <MemberCard k={k} key={m.id} member={m} />
              ))}
            </div>
            <DispatchPanel crew={crew} k={k} />
          </div>
          <ActivityFeed crewId={crewId} k={k} />
        </div>
      )}

      {tab === 'workflow' && <WorkflowBuilder crewId={crewId} k={k} members={crew.members} />}
    </div>
  )
}
