/**
 * Orchestration settings — the dashboard's dispatcher-knobs panel, flat-styled:
 * orchestrator profile, default assignee, auto-decompose, and the profile
 * descriptions the decomposer routes by (save / auto-generate per profile).
 */

import {
  Button,
  Checkbox,
  Codicon,
  ErrorState,
  host,
  Input,
  Loader,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Switch,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useIsMutating } from '@tanstack/react-query'
import { useRef, useState } from 'react'

import {
  $boardSlug,
  $profileDescriptionWriteOwner,
  autoDescribeProfile,
  claimProfileDescriptionWrite,
  fetchOrchestration,
  ORCHESTRATION_KEY,
  ORCHESTRATION_MUTATION_KEY,
  ORCHESTRATION_MUTATION_SCOPE,
  orchestrationKey,
  PROFILE_DESCRIPTION_MUTATION_KEY,
  PROFILE_DESCRIPTION_MUTATION_SCOPE,
  profileQueryOptions,
  PROFILES_KEY,
  profilesKey,
  runProfileDescriptionWrite,
  saveOrchestration,
  saveProfileDescription
} from './api'
import type { KanbanProfile } from './types'
import { errText, FIELD_LABEL, useKanban } from './ui'

const DEFAULT_SENTINEL = '__default__'
const BLOCKED_SENTINEL = '__configured_blocked__'

function ProfilePicker({
  configured,
  disabled,
  label,
  onSave,
  profiles,
  resolved
}: {
  configured: string
  disabled: boolean
  label: string
  onSave: (name: string) => void
  profiles: KanbanProfile[]
  resolved: null | string
}) {
  const k = useKanban()
  const configuredAllowed = Boolean(configured) && profiles.some(profile => profile.name === configured)
  const configuredBlocked = Boolean(configured) && !configuredAllowed
  const normalizedValue = configuredBlocked ? BLOCKED_SENTINEL : configured || DEFAULT_SENTINEL
  const effective = resolved?.trim() || k.none

  return (
    <label className="flex min-w-0 flex-col gap-1">
      <span className={FIELD_LABEL}>{label}</span>
      <Select
        disabled={disabled}
        onValueChange={name => {
          if (name !== BLOCKED_SENTINEL) {
            onSave(name === DEFAULT_SENTINEL ? '' : name)
          }
        }}
        value={normalizedValue}
      >
        <SelectTrigger className="w-44">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {configuredBlocked && (
            <SelectItem disabled value={BLOCKED_SENTINEL}>
              {k.configuredBlocked(configured)}
            </SelectItem>
          )}
          <SelectItem value={DEFAULT_SENTINEL}>{k.defaultResolved(effective)}</SelectItem>
          {profiles.map(profile => (
            <SelectItem key={profile.name} value={profile.name}>
              {profile.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {configuredBlocked && (
        <span className="text-[0.625rem] text-(--ui-text-quaternary)" role="status">
          {k.effectiveFallback(effective)}
        </span>
      )}
    </label>
  )
}

function ProfileDescriptionRow({ disabled, profile }: { disabled: boolean; profile: KanbanProfile }) {
  const k = useKanban()
  const qc = useQueryClient()
  const [draft, setDraft] = useState(profile.description)
  const profileWritePending = useValue($profileDescriptionWriteOwner) !== null
  const rowDisabled = disabled || profileWritePending
  const invalidate = () => qc.invalidateQueries({ queryKey: PROFILES_KEY })

  const save = useMutation({
    mutationFn: ({ description, owner }: { description: string; owner: symbol }) =>
      runProfileDescriptionWrite(owner, () => saveProfileDescription(profile.name, description)),
    mutationKey: PROFILE_DESCRIPTION_MUTATION_KEY,
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: invalidate,
    scope: PROFILE_DESCRIPTION_MUTATION_SCOPE
  })

  const auto = useMutation({
    mutationFn: ({ owner }: { owner: symbol }) =>
      runProfileDescriptionWrite(owner, () => autoDescribeProfile(profile.name)),
    mutationKey: PROFILE_DESCRIPTION_MUTATION_KEY,
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: result => {
      if (result.ok) {
        setDraft(result.description ?? '')

        return invalidate()
      } else {
        host.notify({ kind: 'warning', message: result.reason || k.autoDescribeFailed })
      }
    },
    scope: PROFILE_DESCRIPTION_MUTATION_SCOPE
  })

  const startSave = () => {
    const owner = claimProfileDescriptionWrite()

    if (owner) {
      save.mutate({ description: draft.trim(), owner })
    }
  }

  const startAuto = () => {
    const owner = claimProfileDescriptionWrite()

    if (owner) {
      auto.mutate({ owner })
    }
  }

  return (
    <div className="flex items-center gap-2">
      <span className="w-24 shrink-0 truncate text-[0.75rem] font-medium text-(--ui-text-secondary)">
        {profile.name}
        {profile.is_default && (
          <span className="ml-1 text-[0.625rem] text-(--ui-text-quaternary)">{k.defaultParen}</span>
        )}
      </span>
      <Input
        className="h-7 flex-1 text-[0.71rem]"
        disabled={rowDisabled}
        onChange={event => setDraft(event.target.value)}
        placeholder={k.profileGoodAt}
        value={draft}
      />
      <Button
        disabled={rowDisabled || save.isPending || draft.trim() === profile.description}
        onClick={startSave}
        size="xs"
        variant="outline"
      >
        {k.save}
      </Button>
      {/* Overlay the spinner so the button keeps its "Auto" width — the aux
          model can take a few seconds and a text swap would jump the row. */}
      <Button
        className="relative"
        disabled={rowDisabled || auto.isPending}
        onClick={startAuto}
        size="xs"
        variant="ghost"
      >
        <span className={auto.isPending ? 'invisible' : ''}>{k.auto}</span>
        {auto.isPending && (
          <span className="absolute inset-0 grid place-items-center">
            <Codicon className="animate-spin [animation-duration:1.2s]" name="loading" size="0.75rem" />
          </span>
        )}
      </Button>
    </div>
  )
}

export function OrchestrationPanel() {
  const k = useKanban()
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const profileWritePending = useValue($profileDescriptionWriteOwner) !== null

  const settingsQuery = useQuery({
    queryKey: orchestrationKey(slug),
    queryFn: () => fetchOrchestration(slug)
  })

  const rosterQuery = useQuery(profileQueryOptions(slug))
  const orchestrationWrites = useIsMutating({ exact: true, mutationKey: ORCHESTRATION_MUTATION_KEY })
  const saveOwner = useRef<null | symbol>(null)
  const savePending = orchestrationWrites > 0

  const save = useMutation({
    mutationFn: ({ board, patch }: { board: string; owner: symbol; patch: Record<string, unknown> }) =>
      saveOrchestration(board, patch),
    mutationKey: ORCHESTRATION_MUTATION_KEY,
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSettled: (_data, _error, { owner }) => {
      if (saveOwner.current === owner) {
        saveOwner.current = null
      }
    },
    onSuccess: (result, { board, patch }) => {
      qc.setQueryData(orchestrationKey(board), result)

      const global = ['orchestrator_profile', 'default_assignee', 'auto_decompose', 'auto_promote_children'].some(
        field => Object.hasOwn(patch, field)
      )

      return Promise.all([
        qc.invalidateQueries({ queryKey: global ? ORCHESTRATION_KEY : orchestrationKey(board) }),
        qc.invalidateQueries({ queryKey: profilesKey(board) })
      ])
    },
    scope: ORCHESTRATION_MUTATION_SCOPE
  })

  const settings = settingsQuery.data
  const roster = rosterQuery.data
  const loadError = settingsQuery.error ?? rosterQuery.error

  if (loadError && (!settings || !roster)) {
    return (
      <div className="grid min-h-40 place-items-center border-t border-(--ui-stroke-tertiary) px-4 py-3">
        <ErrorState description={errText(loadError)} title={k.orchestrationLoadError}>
          <Button
            className="justify-self-center"
            onClick={() => void Promise.all([settingsQuery.refetch(), rosterQuery.refetch()])}
            size="sm"
            variant="outline"
          >
            <Codicon name="refresh" size="0.8rem" />
            {k.retry}
          </Button>
        </ErrorState>
      </div>
    )
  }

  if (!settings || !roster) {
    return (
      <div className="grid min-h-40 place-items-center border-t border-(--ui-stroke-tertiary) px-4 py-3">
        <Loader type="lemniscate-bloom" />
      </div>
    )
  }

  const effectiveProfiles = roster.profiles.filter(profile => profile.effective_allowed)
  const inherited = settings.board_allowed_profiles === null

  const savePatch = (patch: Record<string, unknown>) => {
    if (savePending || saveOwner.current) {
      return
    }

    const owner = Symbol('kanban-orchestration-write')
    saveOwner.current = owner
    save.mutate({ board: slug, owner, patch })
  }

  const setInherited = (next: boolean) =>
    savePatch({
      allowed_profiles: next
        ? null
        : roster.profiles
            .filter(profile => profile.machine_allowed && profile.effective_allowed)
            .map(profile => profile.name)
    })

  const setProfileAllowed = (name: string, checked: boolean) => {
    const selected = new Set(settings.board_allowed_profiles ?? [])

    if (checked) {
      selected.add(name)
    } else {
      selected.delete(name)
    }

    savePatch({
      allowed_profiles: roster.profiles
        .filter(profile => profile.machine_allowed && selected.has(profile.name))
        .map(profile => profile.name)
    })
  }

  return (
    <div
      aria-busy={savePending || profileWritePending}
      className="flex flex-col gap-4 border-t border-(--ui-stroke-tertiary) px-4 py-3"
    >
      <div className="flex flex-wrap items-end gap-4">
        <ProfilePicker
          configured={settings.orchestrator_profile}
          disabled={savePending}
          label={k.orchestratorProfile}
          onSave={name => savePatch({ orchestrator_profile: name })}
          profiles={effectiveProfiles}
          resolved={settings.resolved_orchestrator_profile}
        />
        <ProfilePicker
          configured={settings.default_assignee}
          disabled={savePending}
          label={k.defaultAssignee}
          onSave={name => savePatch({ default_assignee: name })}
          profiles={effectiveProfiles}
          resolved={settings.resolved_default_assignee}
        />
        <label className="flex cursor-pointer items-center gap-2 pb-1.5 text-[0.75rem] text-(--ui-text-secondary)">
          <Switch
            aria-label={k.autoDecompose}
            checked={settings.auto_decompose}
            disabled={savePending}
            onCheckedChange={checked => savePatch({ auto_decompose: checked })}
            size="xs"
          />
          {k.autoDecompose}
        </label>
      </div>

      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{k.profilesAllowedBoard}</span>
        <label className="flex cursor-pointer items-center gap-2 text-[0.75rem] text-(--ui-text-secondary)">
          <Checkbox
            aria-label={k.inheritMachinePolicy}
            checked={inherited}
            disabled={savePending}
            onCheckedChange={checked => setInherited(checked === true)}
          />
          {k.inheritMachinePolicy}
        </label>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {roster.profiles.map(profile => {
            const blockedReasonId = `kanban-machine-policy-${profile.name.replace(/[^a-zA-Z0-9_-]/g, '-')}`

            return (
              <label className="flex items-center gap-1.5 text-[0.75rem] text-(--ui-text-secondary)" key={profile.name}>
                <Checkbox
                  aria-describedby={!profile.machine_allowed ? blockedReasonId : undefined}
                  aria-label={profile.machine_allowed ? profile.name : `${profile.name} — ${k.blockedByMachinePolicy}`}
                  checked={inherited ? profile.effective_allowed : profile.board_selected}
                  disabled={inherited || !profile.machine_allowed || savePending}
                  onCheckedChange={checked => setProfileAllowed(profile.name, checked === true)}
                />
                <span>{profile.name}</span>
                {!profile.machine_allowed && (
                  <span className="text-[0.6875rem] text-(--ui-text-quaternary)" id={blockedReasonId}>
                    {k.blockedByMachinePolicy}
                  </span>
                )}
              </label>
            )
          })}
        </div>
        {settings.effective_allowed_profiles.length === 0 && (
          <p className="text-[0.6875rem] font-medium text-(--destructive)" role="alert">
            {k.noWorkersCanRun}
          </p>
        )}
      </div>

      <div className="flex flex-col gap-1.5">
        <span className={FIELD_LABEL}>{k.profileDescriptions}</span>
        <p className="text-[0.6875rem] text-(--ui-text-quaternary)">{k.profileDescriptionsHint}</p>
        {roster.profiles.map(profile => (
          <ProfileDescriptionRow
            disabled={savePending}
            key={`${profile.name}:${profile.description}`}
            profile={profile}
          />
        ))}
      </div>
    </div>
  )
}
