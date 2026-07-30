import { AssistantRuntimeProvider, type ThreadMessage, ThreadPrimitive, useExternalStoreRuntime } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'

import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { GroupApprovalBar } from '@/components/assistant-ui/tool/approval'
import { ToolFallback } from '@/components/assistant-ui/tool/fallback'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { writeClipboardText } from '@/components/ui/copy-button'
import { Input } from '@/components/ui/input'
import { SearchField } from '@/components/ui/search-field'
import { Select, SelectContent, SelectItem, SelectTrigger } from '@/components/ui/select'
import { useI18n } from '@/i18n'
import { $profiles, profileDisplayName, refreshProfiles } from '@/store/profile'
import { $projects, pickProjectFolder, refreshProjects } from '@/store/projects'

import { groupRoomRoute, GROUPS_ROUTE } from '../routes'

import { GroupComposer } from './group-composer'
import type { GroupMessage } from './group-model'
import {
  $groupState,
  beginGroupRoomsRequest,
  cacheGroupRoom,
  cacheSentGroupMessage,
  clearCachedGroupApproval,
  reconcileGroupRooms,
  removeCachedGroupRoom
} from './group-store'
import { createGroupTransport, type GroupRequester, mentionsFromText } from './group-transport'

interface GroupsViewProps {
  copyText?: (text: string) => Promise<void>
  navigate: (path: string) => void
  pickWorkspace?: () => Promise<null | string>
  request: GroupRequester
  roomId: string | null
}

export function GroupsView({ copyText = writeClipboardText, navigate, pickWorkspace = pickProjectFolder, request, roomId }: GroupsViewProps) {
  const { t } = useI18n()
  const copy = t.groups
  const state = useStore($groupState)
  const profiles = useStore($profiles)
  const projects = useStore($projects)
  const room = roomId ? state.rooms[roomId] : undefined
  const transport = useMemo(() => createGroupTransport(request), [request])
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [profileQuery, setProfileQuery] = useState('')
  const [selectedProfiles, setSelectedProfiles] = useState<string[]>([])
  const [workspace, setWorkspace] = useState('')
  const [triggerTokens, setTriggerTokens] = useState(128000)
  const [maxHistoryTokens, setMaxHistoryTokens] = useState(96000)
  const [tailMessageCount, setTailMessageCount] = useState(20)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState('')
  const [historyCursor, setHistoryCursor] = useState<string | null>(null)
  const [hasEarlier, setHasEarlier] = useState(false)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [restoreTarget, setRestoreTarget] = useState<GroupMessage | null>(null)
  const [summaryExpanded, setSummaryExpanded] = useState(false)

  const transcriptRef = useRef<HTMLDivElement>(null)

  const projectWorkspaces = useMemo(
    () => projects.flatMap(project => project.folders.map(folder => ({
      key: `${project.id}:${folder.path}`,
      label: `${project.name} — ${folder.path}`,
      path: folder.path
    }))),
    [projects]
  )

  const isCustomWorkspace = Boolean(workspace) && !projectWorkspaces.some(option => option.path === workspace)

  const visibleProfiles = profiles.filter(profile => {
    const query = profileQuery.trim().toLowerCase()

    return !query || profile.name.toLowerCase().includes(query) || profileDisplayName(profile).toLowerCase().includes(query)
  })

  const profileLabels = useMemo(() => Object.fromEntries(profiles.flatMap(profile => {
    const display = profileDisplayName(profile)

    return display !== profile.name ? [[profile.name, display]] : []
  })), [profiles])

  useEffect(() => {
    if (creating) {void Promise.all([refreshProfiles(), refreshProjects()]).catch(() => undefined)}
  }, [creating])

  useEffect(() => {
    if (roomId) {void refreshProfiles().catch(() => undefined)}
  }, [roomId])

  useEffect(() => {
    let cancelled = false
    setError('')
    const listGeneration = roomId ? null : beginGroupRoomsRequest()
    const load = roomId ? transport.getRoom(roomId) : transport.listRooms()
    void load.then(result => {
      if (cancelled) {return}

      if (listGeneration !== null && 'rooms' in result && Array.isArray(result.rooms)) {
        reconcileGroupRooms(listGeneration, result.rooms)
      }

      if ('room' in result && result.room) {
        cacheGroupRoom(result.room)
        setHistoryCursor(result.cursor ?? null)
        setHasEarlier(result.has_more === true || Boolean(result.cursor))
      }
    }).catch(() => !cancelled && setError(copy.loadFailed))

    if (roomId) {void transport.subscribe(roomId).catch(() => undefined)}

    return () => {
      cancelled = true

      if (roomId) {void transport.unsubscribe(roomId).catch(() => undefined)}
    }
  }, [copy.loadFailed, roomId, transport])

  const create = async (event: FormEvent) => {
    event.preventDefault()

    if (!name.trim() || selectedProfiles.length === 0) {return}

    const result = await transport.createRoom({
      name: name.trim(), profiles: selectedProfiles, ...(workspace ? { workspace } : {}),
      triggerTokens, maxHistoryTokens, tailMessageCount
    })

    if (result.room) {
      cacheGroupRoom(result.room)
      setCreating(false)
      navigate(groupRoomRoute(result.room.id))
    }
  }

  const browseWorkspace = async () => {
    const path = await pickWorkspace()

    if (path) {setWorkspace(path)}
  }

  const loadEarlier = async () => {
    if (!roomId || loadingEarlier || !hasEarlier) {return}
    const viewport = transcriptRef.current
    const previousHeight = viewport?.scrollHeight ?? 0
    const beforeSeq = room?.messages.find(message => message.seq !== undefined)?.seq
    setLoadingEarlier(true)

    try {
      const result = await transport.getRoom(roomId, {
        ...(beforeSeq !== undefined ? { beforeSeq } : {}),
        ...(historyCursor ? { cursor: historyCursor } : {})
      })

      if (result.room) {cacheGroupRoom(result.room)}
      setHistoryCursor(result.cursor ?? null)
      setHasEarlier(result.has_more === true || Boolean(result.cursor))
      requestAnimationFrame(() => {
        if (viewport) {viewport.scrollTop += viewport.scrollHeight - previousHeight}
      })
    } finally {
      setLoadingEarlier(false)
    }
  }

  const sendDraft = () => {
    if (!roomId) {return}

    const content = draft.trim()

    if (!content) {return}

    setDraft('')
    void transport.sendMessage(roomId, content, mentionsFromText(content, room?.profiles ?? [])).then(cacheSentGroupMessage)
  }

  const restoreAndRerun = async () => {
    if (!roomId || restoreTarget?.seq === undefined) {return}

    const result = await transport.rewindMessage(roomId, restoreTarget.seq, restoreTarget.content)

    if (result.room) {
      removeCachedGroupRoom(roomId)
      cacheGroupRoom(result.room)
    }

    setRestoreTarget(null)
  }

  if (!roomId) {
    return <section className="flex h-full min-h-0 flex-col overflow-hidden bg-(--ui-chat-surface-background) pt-(--titlebar-height)">
      <header className="flex items-center justify-between px-6 py-4"><h1 className="text-lg font-semibold">{copy.title}</h1><Button onClick={() => setCreating(true)}>{copy.createRoom}</Button></header>
      {error && <p className="px-6 text-sm text-destructive">{error}</p>}
      {creating && <form className="mx-6 grid max-w-xl gap-3 border-y border-(--ui-stroke-tertiary) py-4" onSubmit={event => void create(event)}>
        <label className="grid gap-1 text-sm">{copy.roomName}<Input aria-label={copy.roomName} onChange={event => setName(event.target.value)} value={name} /></label>
        <fieldset className="grid gap-2"><legend className="text-sm">{copy.profiles}</legend>
          <SearchField aria-label={copy.searchProfiles} containerClassName="w-full" onChange={setProfileQuery} placeholder={copy.searchProfiles} value={profileQuery} />
          <div className="grid max-h-52 gap-1 overflow-auto">{visibleProfiles.map(profile => {
            const checked = selectedProfiles.includes(profile.name)
            const display = profileDisplayName(profile)

            return <label className="flex items-center gap-2 py-1 text-sm" key={profile.name}><input aria-label={`${display} (${profile.name})`} checked={checked} onChange={event => setSelectedProfiles(current => event.target.checked ? [...current, profile.name] : current.filter(name => name !== profile.name))} type="checkbox" /><span>{display}</span>{display !== profile.name && <span className="text-muted-foreground">{profile.name}</span>}</label>
          })}</div>
        </fieldset>
        <label className="grid gap-1 text-sm">{copy.workspace}
          <div className="flex gap-2">
            <Select onValueChange={value => setWorkspace(value === '__none__' ? '' : value)} value={workspace && !isCustomWorkspace ? workspace : '__none__'}>
              <SelectTrigger aria-label={copy.workspace} className="flex-1"><span className="truncate">{workspace || copy.noWorkspace}</span></SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">{copy.noWorkspace}</SelectItem>
                {isCustomWorkspace && <SelectItem value={workspace}>{workspace}</SelectItem>}
                {projectWorkspaces.map(option => <SelectItem key={option.key} value={option.path}>{option.label}</SelectItem>)}
              </SelectContent>
            </Select>
            <Button onClick={() => void browseWorkspace()} type="button" variant="secondary">{copy.browseWorkspace}</Button>
          </div>
        </label>
        <fieldset className="grid gap-2"><legend className="text-sm">{copy.contextPolicy}</legend><div className="grid grid-cols-3 gap-2">
          <label className="grid gap-1 text-xs">{copy.triggerTokens}<Input aria-label={copy.triggerTokens} min={1} onChange={event => setTriggerTokens(Number(event.target.value))} type="number" value={triggerTokens} /></label>
          <label className="grid gap-1 text-xs">{copy.maxHistoryTokens}<Input aria-label={copy.maxHistoryTokens} min={1} onChange={event => setMaxHistoryTokens(Number(event.target.value))} type="number" value={maxHistoryTokens} /></label>
          <label className="grid gap-1 text-xs">{copy.tailMessageCount}<Input aria-label={copy.tailMessageCount} min={0} onChange={event => setTailMessageCount(Number(event.target.value))} type="number" value={tailMessageCount} /></label>
        </div><p className="text-xs text-muted-foreground">{copy.contextPolicyHint}</p></fieldset>
        <div className="flex gap-2"><Button type="submit">{copy.create}</Button><Button onClick={() => setCreating(false)} type="button" variant="secondary">{copy.cancel}</Button></div>
      </form>}
      <div className="min-h-0 flex-1 overflow-auto px-6 py-3">{Object.values(state.rooms).length === 0 ? <p className="text-sm text-muted-foreground">{copy.empty}</p> : <ul className="divide-y divide-(--ui-stroke-tertiary)">{Object.values(state.rooms).map(item => <li key={item.id}><button className="flex w-full items-center justify-between py-3 text-left" onClick={() => navigate(groupRoomRoute(item.id))} type="button"><span><strong className="block">{item.name}</strong><small className="text-muted-foreground">{item.profiles.join(', ')}</small></span><span className="text-xs text-muted-foreground">{item.messages.length}</span></button></li>)}</ul>}</div>
    </section>
  }

  const runningProfiles = new Set(room?.runningProfiles ?? [])
  const contextTrigger = room?.triggerTokens ?? 128000
  const contextBudget = room?.maxHistoryTokens ?? 96000
  const contextTail = room?.tailMessageCount ?? 20
  const workspaceLabel = room?.workspace?.split('/').filter(Boolean).at(-1)

  return <section className="flex h-full min-h-0 flex-col overflow-hidden bg-(--ui-chat-surface-background) pt-(--titlebar-height)">
    <header className="shrink-0 border-b border-(--ui-stroke-tertiary) bg-(--ui-chat-surface-background)/95 backdrop-blur">
      <div className="flex h-14 items-center gap-3 px-5">
        <Button aria-label={copy.back} onClick={() => navigate(GROUPS_ROUTE)} size="icon-sm" title={copy.back} variant="ghost"><Codicon name="arrow-left" /></Button>
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2"><h1 className="truncate text-sm font-semibold">{room?.name ?? roomId}</h1><Badge variant={room?.running ? 'default' : 'muted'}>{copy.agentCount(room?.profiles.length ?? 0)}</Badge>{workspaceLabel && <Badge className="max-w-52 truncate" title={room?.workspace} variant="outline"><Codicon name="folder" />{workspaceLabel}</Badge>}</div>
          <p className="mt-1 truncate text-[0.65rem] text-(--ui-text-tertiary)">{room?.workspace || copy.noWorkspace}</p>
        </div>
        <Button aria-label={copy.stop} disabled={!room?.running} onClick={() => void transport.interrupt(roomId)} size="icon-sm" title={copy.stop} variant="secondary"><Codicon name="debug-stop" /></Button>
        <Button aria-label={copy.delete} onClick={() => void transport.deleteRoom(roomId).then(() => { removeCachedGroupRoom(roomId); navigate(GROUPS_ROUTE) })} size="icon-sm" title={copy.delete} variant="ghost"><Codicon name="trash" /></Button>
      </div>
      <div className="flex min-h-9 items-center gap-1.5 border-t border-(--ui-stroke-tertiary)/60 px-5 py-1.5">
        <span className="mr-1 text-[0.65rem] font-medium text-(--ui-text-tertiary)">{copy.profiles}</span>
        <div className="flex min-w-0 flex-1 flex-wrap gap-1.5">{room?.profiles.map(profile => {
          const running = runningProfiles.has('*') || runningProfiles.has(profile)

          const display = profileLabels[profile]

          return <Button className="h-6 gap-1.5 rounded-md px-2 text-[0.65rem]" key={profile} onClick={() => running && void transport.interrupt(roomId, profile)} title={running ? `${copy.stop} ${display || profile} (@${profile})` : `${display || profile}${display ? ` (@${profile})` : ''}`} type="button" variant="secondary"><span className={running ? 'size-1.5 rounded-full bg-emerald-500 animate-pulse' : 'size-1.5 rounded-full bg-(--ui-text-tertiary)/45'} />{display || profile}{display && <span className="text-(--ui-text-tertiary)">@{profile}</span>}{running && <Codicon name="debug-stop" size="0.65rem" />}</Button>
        })}</div>
        <div className="hidden items-center gap-1.5 text-[0.65rem] text-(--ui-text-tertiary) lg:flex"><span className="font-medium text-(--ui-text-secondary)">{copy.contextPolicy}</span><span title={copy.triggerTokens}>{copy.contextTriggerValue(contextTrigger.toLocaleString())}</span><span>·</span><span title={copy.maxHistoryTokens}>{copy.contextRecentValue(contextBudget.toLocaleString())}</span><span>·</span><span title={copy.tailMessageCount}>{copy.contextMessagesValue(contextTail)}</span></div>
      </div>
    </header>
    {(room?.contextStatus || room?.summary) && <aside className="shrink-0 border-b border-(--ui-stroke-tertiary) bg-(--ui-bg-secondary)/45 px-5 py-1.5">
      <button aria-expanded={summaryExpanded} className="flex w-full items-center gap-2 text-left text-xs" onClick={() => setSummaryExpanded(value => !value)} type="button"><Codicon name={summaryExpanded ? 'chevron-down' : 'chevron-right'} /><strong>{copy.summary}</strong>{room.contextStatus && <span className="truncate text-(--ui-text-tertiary)">{copy.compressed}</span>}{room.compressionCount !== undefined && <Badge size="xs" variant="outline">{copy.compressionCount(room.compressionCount)}</Badge>}</button>
      {summaryExpanded && room.summary && <div className="mt-2 max-h-40 overflow-auto border-l-2 border-primary/25 pl-4 text-xs leading-5 text-(--ui-text-secondary)"><MarkdownTextContent isRunning={false} text={room.summary} /></div>}
    </aside>}
    <div className="min-h-0 flex-1 overflow-auto px-5 py-5" ref={transcriptRef}><div className="mx-auto flex max-w-4xl flex-col gap-6">{hasEarlier && <Button className="self-center" disabled={loadingEarlier} onClick={() => void loadEarlier()} size="sm" variant="textStrong">{loadingEarlier ? copy.loadingEarlier : copy.loadEarlier}</Button>}{room?.messages.map(message => <GroupMessageView copy={copy} copyText={copyText} key={message.id} message={message} requestRestore={setRestoreTarget} respond={async (sessionId, choice) => {
      const result = await transport.respondToApproval(sessionId, choice)
      clearCachedGroupApproval(roomId, message.id)

      return result
    }} respondClarify={(requestId, answer) => transport.respondToClarify(requestId, answer)} />)}</div></div>
    <form className="shrink-0 border-t border-(--ui-stroke-tertiary) bg-(--ui-chat-surface-background)/95 px-5 py-3 backdrop-blur" onSubmit={event => {event.preventDefault(); sendDraft()}}><div className="mx-auto flex max-w-4xl items-end gap-2 rounded-xl border border-(--ui-stroke-secondary) bg-(--ui-bg-secondary)/55 p-2 shadow-sm"><GroupComposer ariaLabel={copy.message} mentionLabel={copy.mentionAgents} onChange={setDraft} onSubmit={sendDraft} profileLabels={profileLabels} profiles={room?.profiles ?? []} value={draft} /><Button aria-label={copy.send} className="size-9 rounded-lg" disabled={!draft.trim()} size="icon" title={copy.send} type="submit"><Codicon name="send" /></Button></div><p className="mx-auto mt-1 max-w-4xl px-1 text-[0.625rem] text-(--ui-text-tertiary)">{copy.composerHint}</p></form>
    <ConfirmDialog cancelLabel={copy.cancel} confirmLabel={copy.restoreConfirm} description={copy.restoreBody} destructive onClose={() => setRestoreTarget(null)} onConfirm={restoreAndRerun} open={Boolean(restoreTarget)} title={copy.restoreTitle} />
  </section>
}

function GroupMessageView({ copy, copyText, message, requestRestore, respond, respondClarify }: { copy: ReturnType<typeof useI18n>['t']['groups']; copyText: (text: string) => Promise<void>; message: GroupMessage; requestRestore: (message: GroupMessage) => void; respond: (sessionId: string, choice: 'once' | 'session' | 'always' | 'deny') => Promise<unknown>; respondClarify: (requestId: string, answer: string) => Promise<unknown> }) {
  const isUser = message.role === 'user'

  return <article className={isUser ? 'group/group-user ml-auto w-fit max-w-[78%]' : 'group/group-agent w-full max-w-full'}><div className={isUser ? 'mb-1 flex items-center justify-end gap-2 px-1 text-[0.65rem] text-(--ui-text-tertiary)' : 'mb-1.5 flex items-center gap-2 text-[0.68rem] text-(--ui-text-tertiary)'}><span className={isUser ? 'grid size-5 place-items-center rounded-md bg-primary/10 text-primary' : 'grid size-5 place-items-center rounded-md bg-(--ui-bg-quaternary) text-(--ui-text-secondary)'}><Codicon name={isUser ? 'account' : 'hubot'} size="0.75rem" /></span><strong className="text-(--ui-text-secondary)">{isUser ? copy.you : message.profile ?? copy.agent}</strong>{message.status === 'streaming' && <span className="flex items-center gap-1"><span className="size-1.5 animate-pulse rounded-full bg-emerald-500" />{copy.working}</span>}</div><div className={isUser ? 'relative rounded-xl border border-(--ui-stroke-tertiary) bg-(--ui-bg-quaternary) px-3.5 py-2.5 pr-16 shadow-xs' : 'ml-7 min-w-0 border-l border-(--ui-stroke-tertiary) pl-4'}><MarkdownTextContent isRunning={message.status === 'streaming'} text={message.content} />{isUser && <div className="absolute right-2 bottom-2 flex gap-1 opacity-0 transition-opacity group-hover/group-user:opacity-100 group-focus-within/group-user:opacity-100"><Button aria-label={copy.copyQuestion} onClick={() => void copyText(message.content)} size="icon-xs" title={copy.copyQuestion} type="button" variant="ghost"><Codicon name="copy" size="0.75rem" /></Button>{message.seq !== undefined && <Button aria-label={copy.restoreCheckpoint} onClick={() => requestRestore(message)} size="icon-xs" title={copy.restoreCheckpoint} type="button" variant="ghost"><Codicon name="discard" size="0.75rem" /></Button>}</div>}<GroupToolParts message={message} />{message.status === 'clarify' && message.clarify && <GroupClarifyPrompt clarify={message.clarify} onRespond={respondClarify} />}</div>{message.status === 'approval' && message.runtimeSessionId && <GroupApprovalBar onRespond={choice => respond(message.runtimeSessionId!, choice)} request={{
        allowPermanent: message.approval?.allowPermanent,
        choices: message.approval?.choices,
        command: message.approval?.command ?? message.content,
        description: message.approval?.description || copy.approval,
        sessionId: message.runtimeSessionId,
        smartDenied: message.approval?.smartDenied
      }} />}</article>
}

function GroupClarifyPrompt({ clarify, onRespond }: { clarify: NonNullable<GroupMessage['clarify']>; onRespond: (requestId: string, answer: string) => Promise<unknown> }) {
  const { t } = useI18n()
  const copy = t.assistant.clarify
  const [selected, setSelected] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const answer = selected ?? draft.trim()

  const submit = async () => {
    if (!answer || submitting) {return}
    setSubmitting(true)

    try {
      await onRespond(clarify.requestId, answer)
    } catch {
      setSubmitting(false)
    }
  }

  return <div className="mt-3 grid gap-2 rounded-lg border border-primary/20 bg-primary/[0.035] p-3" data-slot="group-clarify">
    <div className="flex items-start gap-2"><Codicon className="mt-0.5 text-primary" name="question" /><strong className="text-sm leading-5">{clarify.question}</strong></div>
    {clarify.choices && clarify.choices.length > 0 && <div className="grid gap-1">{clarify.choices.map((choice, index) => <button aria-label={choice} className={selected === choice ? 'flex items-center gap-2 rounded-md bg-primary/10 px-2 py-1.5 text-left text-sm text-primary' : 'flex items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-(--chrome-action-hover)'} key={choice} onClick={() => {setSelected(choice); setDraft('')}} type="button"><Badge size="xs" variant={selected === choice ? 'default' : 'outline'}>{String.fromCharCode(65 + index)}</Badge>{choice}</button>)}</div>}
    <Input aria-label={copy.other} onChange={event => {setDraft(event.target.value); setSelected(null)}} placeholder={clarify.choices?.length ? copy.other : copy.placeholder} value={draft} />
    <div className="flex justify-end"><Button disabled={!answer || submitting} onClick={() => void submit()} size="sm" type="button">{copy.continueLabel}</Button></div>
  </div>
}

function GroupToolParts({ message }: { message: GroupMessage }) {
  const toolParts = message.parts.filter(part => part.type === 'tool-call')

  const runtimeMessage = useMemo<ThreadMessage>(() => ({
    id: message.id,
    role: 'assistant',
    content: toolParts,
    status: message.status === 'streaming' ? { type: 'running' } : { type: 'complete', reason: 'stop' },
    createdAt: new Date(message.createdAt ?? Date.now()),
    metadata: { unstable_state: null, unstable_annotations: [], unstable_data: [], steps: [], custom: {} }
  } as ThreadMessage), [message.createdAt, message.id, message.status, toolParts])

  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [runtimeMessage],
    isRunning: message.status === 'streaming',
    onNew: async () => {}
  })

  if (toolParts.length === 0) {return null}

  return <AssistantRuntimeProvider runtime={runtime}>
    <ThreadPrimitive.Messages>{() => <>{toolParts.map(part => <ToolFallback addResult={() => undefined} args={part.args ?? {}} argsText={'argsText' in part ? part.argsText ?? '{}' : '{}'} key={part.toolCallId ?? 'tool'} respondToApproval={() => undefined} result={'result' in part ? part.result : undefined} resume={() => undefined} status={message.status === 'streaming' ? { type: 'running' } : { type: 'complete' }} toolCallId={part.toolCallId ?? 'tool'} toolName={part.toolName ?? 'tool'} type="tool-call" />)}</>}</ThreadPrimitive.Messages>
  </AssistantRuntimeProvider>
}
