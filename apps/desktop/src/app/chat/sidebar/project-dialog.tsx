import { useStore } from '@nanostores/react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { GenerateButton } from '@/components/ui/generate-button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Tip } from '@/components/ui/tooltip'
import type { DesktopRegistryConnection } from '@/global'
import { useI18n } from '@/i18n'
import { Check, Cloud, Monitor, Network, Terminal } from '@/lib/icons'
import { type ProjectIdeaTemplate, randomIdeaTemplates } from '@/lib/project-idea-templates'
import { selectableCardClass } from '@/lib/selectable-card'
import { cn } from '@/lib/utils'
import { $activeConnectionId, $connectionsRegistry } from '@/store/connections'
import { notifyError } from '@/store/notifications'
import {
  $projectDialog,
  addProjectFolder,
  closeProjectDialog,
  connectionIdForProjectId,
  createProject,
  generateProjectIdea,
  pickProjectFolder,
  renameProject
} from '@/store/projects'

const GATEWAY_KIND_ICON: Record<DesktopRegistryConnection['kind'], typeof Monitor> = {
  cloud: Cloud,
  local: Monitor,
  remote: Network,
  ssh: Terminal
}

// Single dialog mounted once in the sidebar; it renders create / rename /
// add-folder flows driven by the $projectDialog atom. Folders are chosen via
// the remote-aware picker pinned to the selected gateway (create) or the
// project's stamped connection (add-folder).
export function ProjectDialog() {
  const { t } = useI18n()
  const p = t.sidebar.projects
  const state = useStore($projectDialog)
  const registry = useStore($connectionsRegistry)
  const activeConnectionId = useStore($activeConnectionId)
  const open = state !== null
  const mode = state?.mode ?? 'create'

  const gateways = useMemo(() => registry?.connections ?? [], [registry])
  const showGatewayPicker = mode === 'create' && gateways.length > 1

  const kindLabels: Record<DesktopRegistryConnection['kind'], string> = {
    cloud: t.settings.connections.kindCloud,
    local: t.settings.connections.kindLocal,
    remote: t.settings.connections.kindRemote,
    ssh: t.settings.connections.kindSsh
  }

  const [name, setName] = useState('')
  const [folders, setFolders] = useState<string[]>([])
  const [idea, setIdea] = useState('')
  const [templates, setTemplates] = useState<ProjectIdeaTemplate[]>([])
  const [generatingIdea, setGeneratingIdea] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [gatewayId, setGatewayId] = useState('local')
  const nameRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (open) {
      setName(state?.name ?? '')
      setFolders([])
      setIdea('')
      setTemplates(randomIdeaTemplates())
      setGeneratingIdea(false)
      setSubmitting(false)
      setGatewayId(activeConnectionId || gateways.find(conn => conn.kind === 'local')?.id || gateways[0]?.id || 'local')

      if (mode !== 'add-folder') {
        window.setTimeout(() => nameRef.current?.select(), 0)
      }
    }
  }, [open, mode, state?.name, activeConnectionId, gateways])

  const onOpenChange = (next: boolean) => {
    if (!next) {
      closeProjectDialog()
    }
  }

  // One submit beat for every flow: guard re-entry, run the write, close on
  // success, surface a toast on failure. Callers pass only the write.
  const runSubmit = async (write: () => Promise<unknown>) => {
    if (submitting) {
      return
    }

    setSubmitting(true)

    try {
      await write()
      closeProjectDialog()
    } catch (err) {
      notifyError(err, p.createFailed)
    } finally {
      setSubmitting(false)
    }
  }

  const folderConnectionId = () => {
    if (mode === 'add-folder' && state?.projectId) {
      return connectionIdForProjectId(state.projectId) || activeConnectionId || 'local'
    }

    return gatewayId
  }

  const pickFolder = async () => {
    try {
      const dir = await pickProjectFolder(folderConnectionId())

      if (!dir) {
        return
      }

      const projectId = state?.projectId

      if (mode === 'add-folder' && projectId) {
        await runSubmit(() => addProjectFolder(projectId, dir))

        return
      }

      setFolders(prev => (prev.includes(dir) ? prev : [...prev, dir]))
    } catch (err) {
      notifyError(err, p.createFailed)
    }
  }

  const submit = async () => {
    const trimmed = name.trim()
    const projectId = state?.projectId

    if (mode === 'rename' && projectId) {
      if (trimmed) {
        await runSubmit(() => renameProject(projectId, trimmed))
      }

      return
    }

    // A project owns sessions by folder (cwd-prefix), so creation requires at
    // least one — a folder-less project couldn't hold a session anyway.
    if (mode === 'create' && trimmed && folders.length) {
      await runSubmit(() =>
        createProject({
          connectionId: gatewayId,
          folders,
          idea: idea.trim() || undefined,
          name: trimmed,
          use: true
        })
      )
    }
  }

  const generateIdea = async () => {
    if (generatingIdea) {
      return
    }

    setGeneratingIdea(true)

    try {
      const text = await generateProjectIdea(name)

      if (text) {
        setIdea(text)
      }
    } finally {
      setGeneratingIdea(false)
    }
  }

  const selectGateway = (id: string) => {
    if (id === gatewayId) {
      return
    }

    setGatewayId(id)
    setFolders([])
  }

  const title = mode === 'rename' ? p.renameTitle : mode === 'add-folder' ? p.addFolderTitle : p.createTitle

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-md" onInteractOutside={event => event.preventDefault()}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {mode === 'create' && <DialogDescription>{p.createDesc}</DialogDescription>}
        </DialogHeader>

        {mode !== 'add-folder' && (
          <Input
            autoFocus
            disabled={submitting}
            onChange={event => setName(event.target.value)}
            onKeyDown={event => {
              if (event.key === 'Enter') {
                event.preventDefault()
                void submit()
              } else if (event.key === 'Escape') {
                onOpenChange(false)
              }
            }}
            placeholder={p.namePlaceholder}
            ref={nameRef}
            value={name}
          />
        )}

        {showGatewayPicker && (
          <div className="flex flex-col gap-1.5">
            <span className="text-[0.6875rem] font-medium text-(--ui-text-tertiary)">{p.gatewayLabel}</span>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {gateways.map(conn => {
                const Icon = GATEWAY_KIND_ICON[conn.kind]
                const active = conn.id === gatewayId

                return (
                  <button
                    aria-pressed={active}
                    className={cn(
                      'flex w-full flex-col p-3 text-left disabled:cursor-not-allowed disabled:opacity-50',
                      selectableCardClass({ active, prominent: true })
                    )}
                    disabled={submitting}
                    key={conn.id}
                    onClick={() => selectGateway(conn.id)}
                    type="button"
                  >
                    <div className="flex items-center gap-1.5">
                      <Icon className="size-3.5 shrink-0 text-muted-foreground" />
                      <span className="min-w-0 truncate text-[0.8125rem] font-medium">{conn.label}</span>
                      {active ? <Check className="ml-auto size-3.5 shrink-0 text-primary" /> : null}
                    </div>
                    <p className="mt-1.5 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">
                      {kindLabels[conn.kind]}
                    </p>
                  </button>
                )
              })}
            </div>
          </div>
        )}

        {mode === 'create' && (
          <div className="flex flex-col gap-1.5">
            <span className="text-[0.6875rem] font-medium text-(--ui-text-tertiary)">{p.foldersLabel}</span>
            {folders.length === 0 ? (
              <span className="text-[0.75rem] text-(--ui-text-quaternary)">{p.noFolders}</span>
            ) : (
              <ul className="flex flex-col gap-1">
                {folders.map((folder, index) => (
                  <li
                    className={cn(
                      'flex items-center gap-2 rounded-md bg-(--ui-control-hover-background) px-2 py-1 text-[0.75rem]'
                    )}
                    key={folder}
                  >
                    <Codicon className="shrink-0 text-(--ui-text-tertiary)" name="folder" size="0.75rem" />
                    <span className="min-w-0 flex-1 truncate" title={folder}>
                      {folder}
                    </span>
                    {index === 0 && (
                      <span className="shrink-0 text-[0.625rem] uppercase text-(--ui-text-quaternary)">
                        {p.primaryBadge}
                      </span>
                    )}
                    <Tip label={p.removeFolder}>
                      <Button
                        aria-label={p.removeFolder}
                        className="size-5 shrink-0 text-(--ui-text-quaternary) hover:text-foreground"
                        onClick={() => setFolders(prev => prev.filter(f => f !== folder))}
                        size="icon-xs"
                        type="button"
                        variant="ghost"
                      >
                        <Codicon name="close" size="0.75rem" />
                      </Button>
                    </Tip>
                  </li>
                ))}
              </ul>
            )}
            <Button
              className="self-start"
              disabled={submitting}
              onClick={() => void pickFolder()}
              size="sm"
              type="button"
              variant="ghost"
            >
              <Codicon name="add" size="0.75rem" />
              {p.addFolder}
            </Button>
          </div>
        )}

        {mode === 'create' && (
          <div className="flex flex-col gap-1.5">
            <span className="text-[0.6875rem] font-medium text-(--ui-text-tertiary)">{p.ideaLabel}</span>
            <div className="relative">
              <Textarea
                className="min-h-20 pr-8 text-[0.8125rem]"
                disabled={submitting}
                onChange={event => setIdea(event.target.value)}
                placeholder={p.ideaPlaceholder}
                value={idea}
              />
              <GenerateButton
                className="absolute top-1 right-1"
                disabled={submitting}
                generating={generatingIdea}
                generatingLabel={p.ideaGenerating}
                label={p.ideaGenerate}
                onGenerate={() => void generateIdea()}
              />
            </div>
            <div className="flex flex-wrap items-center gap-1">
              {templates.map(template => (
                <button
                  className="flex items-center gap-1 rounded-full border border-(--ui-stroke-tertiary) px-2 py-0.5 text-[0.6875rem] text-(--ui-text-secondary) transition-colors hover:border-(--ui-stroke-secondary) hover:bg-(--ui-control-hover-background) hover:text-foreground disabled:opacity-50"
                  disabled={submitting}
                  key={template.label}
                  onClick={() => setIdea(template.idea)}
                  type="button"
                >
                  <span aria-hidden>{template.emoji}</span>
                  {template.label}
                </button>
              ))}
              <Tip label={p.ideaShuffle}>
                <Button
                  aria-label={p.ideaShuffle}
                  className="size-5 text-(--ui-text-quaternary) hover:text-foreground"
                  disabled={submitting}
                  onClick={() => setTemplates(randomIdeaTemplates())}
                  size="icon-xs"
                  type="button"
                  variant="ghost"
                >
                  <Codicon name="refresh" size="0.75rem" />
                </Button>
              </Tip>
            </div>
          </div>
        )}

        {mode === 'add-folder' && (
          <Button disabled={submitting} onClick={() => void pickFolder()} type="button">
            <Codicon name="folder-opened" size="0.875rem" />
            {p.addFolder}
          </Button>
        )}

        {mode !== 'add-folder' && (
          <DialogFooter>
            <Button disabled={submitting} onClick={() => onOpenChange(false)} type="button" variant="ghost">
              {t.common.cancel}
            </Button>
            <Button
              disabled={submitting || !name.trim() || (mode === 'create' && folders.length === 0)}
              onClick={() => void submit()}
              type="button"
            >
              {mode === 'rename' ? t.common.save : p.create}
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}
