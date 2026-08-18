import { useCallback, useEffect, useRef, useState } from 'react'

import { OpenRouterModelInput } from '@/app/settings/openrouter-model-input'
import { OpenRouterRoutingField } from '@/app/settings/openrouter-routing-field'
import { ActionStatus } from '@/components/ui/action-status'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { Field, FieldHint } from '@/components/ui/field'
import { SanitizedInput } from '@/components/ui/sanitized-input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import {
  createProfile,
  getGlobalModelInfo,
  getGlobalModelOptions,
  getHermesConfigRecord,
  getOpenRouterEndpoints,
  type HermesConfigRecord,
  type ModelOptionProvider,
  type OpenRouterEndpoint,
  saveHermesConfig,
  updateProfileSoul
} from '@/hermes'
import { useI18n } from '@/i18n'
import { AlertTriangle } from '@/lib/icons'
import {
  isOpenRouterProvider,
  openRouterRoutingDraft,
  type OpenRouterRoutingDraft,
  updateOpenRouterRoutingConfig
} from '@/lib/openrouter-routing'
import { slug } from '@/lib/sanitize'
import { setMainModelAssignment } from '@/store/cron-model-impact'
import type { ProfileInfo } from '@/types/hermes'

const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

const EMPTY_ROUTING_DRAFT: OpenRouterRoutingDraft = {
  allowFallbacks: false,
  blockedTags: [],
  providerTag: '',
  quantization: ''
}

export function isValidProfileName(name: string): boolean {
  return PROFILE_NAME_RE.test(name.trim())
}

export function CreateProfileDialog({
  onClose,
  onCreated,
  open,
  profiles = []
}: {
  onClose: () => void
  onCreated?: (name: string) => Promise<void> | void
  open: boolean
  profiles?: ProfileInfo[]
}) {
  const { t } = useI18n()
  const p = t.profiles
  const m = t.settings.model
  const [name, setName] = useState('')
  const [cloneFrom, setCloneFrom] = useState<null | string>('default')
  const [soul, setSoul] = useState('')
  const [status, setStatus] = useState<'done' | 'idle' | 'saving'>('idle')
  const [error, setError] = useState<null | string>(null)
  const [providers, setProviders] = useState<ModelOptionProvider[]>([])
  const [provider, setProvider] = useState('')
  const [model, setModel] = useState('')
  const [sourceConfig, setSourceConfig] = useState<HermesConfigRecord>({})
  const [routingDraft, setRoutingDraft] = useState<OpenRouterRoutingDraft>(EMPTY_ROUTING_DRAFT)
  const [routingEndpoints, setRoutingEndpoints] = useState<OpenRouterEndpoint[]>([])
  const [routingError, setRoutingError] = useState('')
  const [routingLoading, setRoutingLoading] = useState(false)
  const [routingManual, setRoutingManual] = useState(false)
  const sourceGeneration = useRef(0)
  const endpointGeneration = useRef(0)

  // Generation refs deliberately invalidate async work across dialog resets.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    if (!open) {
      return
    }

    setName('')
    setCloneFrom('default')
    setSoul('')
    setError(null)
    setStatus('idle')
    setProviders([])
    setProvider('')
    setModel('')
    setSourceConfig({})
    setRoutingDraft(EMPTY_ROUTING_DRAFT)
    setRoutingEndpoints([])
    setRoutingError('')
    setRoutingManual(false)
    sourceGeneration.current += 1
    endpointGeneration.current += 1
  }, [open])

  // Async source loading is guarded by a generation token, not mirrored state.
  // eslint-disable-next-line no-restricted-syntax
  useEffect(() => {
    if (!open) {
      return
    }

    const generation = sourceGeneration.current + 1
    sourceGeneration.current = generation
    const scope = cloneFrom

    void Promise.all([
      getGlobalModelInfo(scope),
      getGlobalModelOptions({ explicitOnly: true }, scope),
      getHermesConfigRecord(scope)
    ])
      .then(([info, options, config]) => {
        if (sourceGeneration.current !== generation) {
          return
        }

        const available = (options.providers ?? []).filter(row => row.authenticated !== false)
        const selectedProvider = info.provider || available[0]?.slug || ''
        const selectedModels = available.find(row => row.slug === selectedProvider)?.models ?? []
        const selectedModel = info.model || selectedModels[0] || ''
        setProviders(available)
        setProvider(selectedProvider)
        setModel(selectedModel)
        setSourceConfig(config)
        setRoutingDraft(openRouterRoutingDraft(config, selectedModel))
      })
      .catch(err => {
        if (sourceGeneration.current === generation) {
          setError(err instanceof Error ? err.message : p.failedLoad)
        }
      })
  }, [cloneFrom, open, p.failedLoad])

  const loadRoutingEndpoints = useCallback(
    async (refresh = false) => {
      const generation = endpointGeneration.current + 1
      endpointGeneration.current = generation

      if (!isOpenRouterProvider(provider) || !model) {
        setRoutingEndpoints([])
        setRoutingError('')
        setRoutingLoading(false)

        return
      }

      setRoutingEndpoints([])
      setRoutingError('')
      setRoutingLoading(true)

      try {
        const result = await getOpenRouterEndpoints(model, {
          profile: cloneFrom,
          ...(refresh ? { refresh: true } : {})
        })

        if (endpointGeneration.current === generation) {
          setRoutingEndpoints(result.endpoints ?? [])
        }
      } catch (err) {
        if (endpointGeneration.current === generation) {
          setRoutingError(err instanceof Error ? err.message : String(err))
        }
      } finally {
        if (endpointGeneration.current === generation) {
          setRoutingLoading(false)
        }
      }
    },
    [cloneFrom, model, provider]
  )

  useEffect(() => {
    setRoutingDraft(openRouterRoutingDraft(sourceConfig, model))
    setRoutingManual(false)
    void loadRoutingEndpoints()
  }, [loadRoutingEndpoints, model, sourceConfig])

  const trimmed = name.trim()
  const invalid = trimmed !== '' && !isValidProfileName(trimmed)
  const busy = status === 'saving' || status === 'done'
  const providerModels = providers.find(row => row.slug === provider)?.models ?? []

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()

    if (!trimmed || invalid) {
      setError(invalid ? p.invalidName(p.nameHint) : p.nameRequired)

      return
    }

    setStatus('saving')
    setError(null)

    try {
      await createProfile({ name: trimmed, clone_from: cloneFrom })

      if (provider && model) {
        await setMainModelAssignment({ provider, model }, trimmed)
      }

      if (isOpenRouterProvider(provider) && model) {
        const targetConfig = await getHermesConfigRecord(trimmed)
        await saveHermesConfig(updateOpenRouterRoutingConfig(targetConfig, model, routingDraft), trimmed)
      }

      if (soul.trim()) {
        await updateProfileSoul(trimmed, soul)
      }

      await onCreated?.(trimmed)
      setStatus('done')
      window.setTimeout(onClose, 800)
    } catch (err) {
      setStatus('idle')
      setError(err instanceof Error ? err.message : p.failedCreate)
    }
  }

  return (
    <Dialog onOpenChange={value => !value && !busy && onClose()} open={open}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{p.newProfile}</DialogTitle>
          <DialogDescription>{p.createDesc}</DialogDescription>
        </DialogHeader>

        <form className="grid gap-4" onSubmit={handleSubmit}>
          <Field htmlFor="new-profile-name" label={p.nameLabel}>
            <SanitizedInput
              aria-invalid={invalid}
              autoFocus
              id="new-profile-name"
              onValueChange={setName}
              placeholder="my-profile"
              sanitize={slug}
              value={name}
            />
            <FieldHint error={invalid}>{p.nameHint}</FieldHint>
          </Field>

          <Field htmlFor="new-profile-clone-from" label={p.cloneFrom}>
            <Select
              onValueChange={value => setCloneFrom(value === '__none__' ? null : value)}
              value={cloneFrom ?? '__none__'}
            >
              <SelectTrigger className="h-9 rounded-md" id="new-profile-clone-from">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">{p.cloneFromNone}</SelectItem>
                {profiles.map(profileOption => (
                  <SelectItem key={profileOption.name} value={profileOption.name}>
                    {profileOption.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <FieldHint>{p.cloneFromDesc}</FieldHint>
          </Field>

          {providers.length > 0 && (
            <div className="grid gap-3 rounded-md border border-border/70 p-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <Field htmlFor="new-profile-provider" label={m.provider}>
                  <Select
                    onValueChange={value => {
                      const models = providers.find(row => row.slug === value)?.models ?? []
                      setProvider(value)
                      setModel(models[0] ?? '')
                    }}
                    value={provider}
                  >
                    <SelectTrigger id="new-profile-provider">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {providers.map(row => (
                        <SelectItem key={row.slug} value={row.slug}>
                          {row.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>

                <Field htmlFor="new-profile-model" label={m.model}>
                  {isOpenRouterProvider(provider) ? (
                    <OpenRouterModelInput
                      hint={m.openrouterModelShapeHint}
                      label={m.openrouterModelInput}
                      onChange={setModel}
                      options={providerModels}
                      value={model}
                    />
                  ) : (
                    <Select onValueChange={setModel} value={model}>
                      <SelectTrigger id="new-profile-model">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {providerModels.map(option => (
                          <SelectItem className="font-mono" key={option} value={option}>
                            {option}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  )}
                </Field>
              </div>

              {isOpenRouterProvider(provider) && model && (
                <OpenRouterRoutingField
                  copy={m.openrouterRouting}
                  draft={routingDraft}
                  endpoints={routingEndpoints}
                  error={routingError}
                  loading={routingLoading}
                  manual={routingManual}
                  onDraftChange={setRoutingDraft}
                  onManualChange={setRoutingManual}
                  onRefresh={() => void loadRoutingEndpoints(true)}
                />
              )}
            </div>
          )}

          <Field htmlFor="new-profile-soul" label="SOUL.md" optional optionalLabel={p.soulOptional}>
            <Textarea
              className="min-h-28 font-mono text-xs leading-5"
              id="new-profile-soul"
              onChange={event => setSoul(event.target.value)}
              placeholder={p.soulPlaceholder(cloneFrom ? p.soulPlaceholderCloned : p.soulPlaceholderEmpty)}
              value={soul}
            />
          </Field>

          {error && (
            <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <DialogFooter>
            <Button disabled={busy} onClick={onClose} type="button" variant="ghost">
              {t.common.cancel}
            </Button>
            <Button disabled={busy || !trimmed || invalid} type="submit">
              <ActionStatus busy={p.creating} done={p.created} idle={p.createAction} state={status} />
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
