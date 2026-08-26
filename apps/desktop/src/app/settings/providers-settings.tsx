import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  FEATURED_ID,
  FeaturedProviderRow,
  FireworksProviderRow,
  OpenRouterProviderRow,
  ProviderRow,
  providerTitle,
  sortProviders
} from '@/components/onboarding'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { RowButton } from '@/components/ui/row-button'
import { SearchField } from '@/components/ui/search-field'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import {
  getCredentialPool,
  listOAuthProviders,
  removeCredentialPoolEntry,
  renameCredentialPoolEntry,
  setCredentialPoolStrategy
} from '@/hermes'
import { useI18n } from '@/i18n'
import { ChevronDown, ChevronRight, KeyRound, Trash2 } from '@/lib/icons'
import { normalize } from '@/lib/text'
import { cn } from '@/lib/utils'
import { confirm } from '@/store/confirm'
import { notifyError } from '@/store/notifications'
import { $desktopOnboarding, startManualLocalEndpoint, startManualProviderOAuth } from '@/store/onboarding'
import { $settingsRequestProfile } from '@/store/settings-scope'
import type { CredentialPoolEntry, EnvVarInfo, OAuthProvider } from '@/types/hermes'

import { isKeyVar, ProviderKeyRows } from './credential-key-ui'
import { CustomEndpointsSettings } from './custom-endpoints-settings'
import { SettingsCategoryHeading, useEnvCredentials } from './env-credentials'
import { providerGroup, providerMeta, providerPriority } from './helpers'
import { ListRow, SettingsContent, SettingsSkeleton } from './primitives'
import { SettingsProfileScope } from './profile-scope'


// Sub-views surfaced as a sidebar subnav: account sign-in vs raw API keys.
export const PROVIDER_VIEWS = ['accounts', 'keys', 'custom-endpoints'] as const

export type ProviderView = (typeof PROVIDER_VIEWS)[number]

// Group the env catalog by provider — one ListRow per vendor plus optional
// advanced overrides (base URL, region, etc.). Groups without a key field are
// skipped.
//
// Grouping key precedence:
//   1. Backend `provider_label` / `provider` (from the unified provider catalog
//      in hermes_cli/provider_catalog.py) — the SAME provider identity
//      `hermes model` uses. This is authoritative: a provider tagged by the
//      backend always renders a card, even with no PROVIDER_GROUPS row.
//   2. Desktop prefix match (`providerGroup`) — legacy fallback for provider
//      env vars that predate the backend tagging.
// Only entries that resolve to neither (the "Other" bucket) are skipped.
function buildProviderKeyGroups(vars: Record<string, EnvVarInfo>): ProviderKeyGroup[] {
  const buckets = new Map<string, [string, EnvVarInfo][]>()

  for (const [key, info] of Object.entries(vars)) {
    if (info.category !== 'provider') {
      continue
    }

    // Prefer the backend-supplied provider label/id so the Keys tab groups by
    // the same identity the CLI picker uses; fall back to the prefix guess.
    const name = info.provider_label?.trim() || info.provider?.trim() || providerGroup(key)

    if (name === 'Other') {
      continue
    }

    buckets.set(name, [...(buckets.get(name) ?? []), [key, info]])
  }

  const groups: ProviderKeyGroup[] = []

  for (const [name, entries] of buckets) {
    const primary = entries.find(([k, i]) => !i.advanced && isKeyVar(k, i)) ?? entries.find(([k, i]) => isKeyVar(k, i))

    if (!primary) {
      continue
    }

    // Presentation overlay (priority, blurb, docs) is keyed by the prefix-based
    // group name; when the backend introduced this provider it may have no
    // overlay entry, so fall back to the backend/env metadata for display.
    const meta = providerMeta(name)

    groups.push({
      // Advanced = the provider's non-key knobs (base URL, region, deployment).
      // Skip redundant alias key vars (e.g. ANTHROPIC_TOKEN vs ANTHROPIC_API_KEY)
      // so we never render a second "Paste key" input — unless one is already
      // set, in which case keep it visible so it stays clearable.
      advanced: entries
        .filter(([k, i]) => k !== primary[0] && (!isKeyVar(k, i) || i.is_set))
        .sort(([a], [b]) => a.localeCompare(b)),
      description: meta?.description ?? primary[1].description,
      docsUrl: meta?.docsUrl ?? primary[1].url ?? undefined,
      hasAnySet: entries.some(([, i]) => i.is_set),
      name,
      primary,
      priority: providerPriority(name)
    })
  }

  return groups.sort((a, b) => a.priority - b.priority || a.name.localeCompare(b.name))
}

// Deliberately a near-1:1 replica of the first-run onboarding picker
// (`Picker` in desktop-onboarding-overlay): same recommended card, same
// Fireworks #2 quick-key row, same provider rows, same "Other providers"
// disclosure, same OpenRouter quick-key row, and the same bottom-right
// "I have an API key" affordance. The leaf cards are the exact shared
// components, so the two surfaces stay visually identical. Selecting a
// provider hands off to the shared onboarding overlay, which runs that
// provider's real sign-in flow; the key affordances open the API-key
// catalog below.
function OAuthPicker({
  onWantApiKey,
  onSelect,
  providers
}: {
  onWantApiKey: () => void
  onSelect: (provider: OAuthProvider) => void
  providers: OAuthProvider[]
}) {
  const { t } = useI18n()
  const p = t.settings.providers
  const [showAll, setShowAll] = useState(false)
  const ordered = useMemo(() => sortProviders(providers), [providers])

  if (ordered.length === 0) {
    return null
  }

  const select = onSelect

  const featured = ordered.find(p => p.id === FEATURED_ID && !p.status?.logged_in) ?? null
  const rest = featured ? ordered.filter(p => p.id !== FEATURED_ID) : ordered
  const others = rest.filter(p => !p.status?.logged_in)
  const externallyManaged = rest.filter(p => p.status?.logged_in && p.flow === 'external')
  const collapsible = others.length > 0
  const showOthers = !collapsible || showAll

  return (
    <section className="mb-5 grid gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <SettingsCategoryHeading icon={KeyRound} title={p.connectAccount} />
        <Button
          className="text-[length:var(--conversation-caption-font-size)]"
          onClick={onWantApiKey}
          size="inline"
          type="button"
          variant="textStrong"
        >
          {p.haveApiKey}
        </Button>
      </div>
      <p className="-mt-2 mb-1 text-[length:var(--conversation-caption-font-size)] leading-(--conversation-caption-line-height) text-(--ui-text-tertiary)">
        {p.intro}
      </p>
      {featured && <FeaturedProviderRow onSelect={select} provider={featured} />}
      {/* Slot #2 — always visible, matching onboarding / CANONICAL_PROVIDERS. */}
      <FireworksProviderRow onClick={onWantApiKey} />

      {externallyManaged.length > 0 && (
        <div className="grid gap-1">
          <p className="mt-3 px-0.5 text-[length:var(--conversation-caption-font-size)] font-medium text-(--ui-text-tertiary)">Managed externally</p>
          {externallyManaged.map(provider => (
            <div className="rounded-[6px] px-3 py-2.5" key={provider.id}>
              <p className="text-[length:var(--conversation-text-font-size)] font-semibold">{providerTitle(provider)}</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">{t.settings.providers.removeExternalGeneric(providerTitle(provider))}</p>
            </div>
          ))}
        </div>
      )}

      {showOthers && (
        <>
          {others.map(p => (
            <ProviderRow key={p.id} onSelect={select} provider={p} />
          ))}
          <OpenRouterProviderRow onClick={onWantApiKey} />
        </>
      )}
      {collapsible && (
        <Button
          className="py-1 text-[length:var(--conversation-caption-font-size)]"
          onClick={() => setShowAll(v => !v)}
          size="inline"
          type="button"
          variant="text"
        >
          {showAll ? p.collapse : p.connectAnother}
          <ChevronDown className={cn('size-3.5 transition', showAll && 'rotate-180')} />
        </Button>
      )}
    </section>
  )
}

function OAuthAccountRows({
  accounts,
  onAdd,
  onRemove,
  onRename,
  onStrategyChange,
  providers,
  strategies
}: {
  accounts: Array<{ entry: CredentialPoolEntry; provider: string }>
  onAdd: (provider: OAuthProvider) => void
  onRemove: (provider: string, entry: CredentialPoolEntry) => void
  onRename: (provider: string, entry: CredentialPoolEntry, label: string) => void
  onStrategyChange: (provider: string, strategy: string) => void
  providers: OAuthProvider[]
  strategies: Record<string, string>
}) {
  const [editing, setEditing] = useState<null | { entry: CredentialPoolEntry; provider: string }>(null)
  const [label, setLabel] = useState('')
  const { t } = useI18n()
  const copy = t.settings.providers

  if (accounts.length === 0) {
    return null
  }

  return (
    <section className="mb-5 grid gap-2">
      <SettingsCategoryHeading icon={KeyRound} title="Subscriptions" />
      {[...new Set(accounts.map(account => account.provider))].map(provider => {
        const providerAccounts = accounts.filter(account => account.provider === provider)
        const oauthProvider = providers.find(candidate => candidate.id === provider)
        const strategy = strategies[provider] ?? 'fill_first'

        return <div className="grid gap-1" key={provider}>
          <p className="px-3 pt-2 text-[length:var(--conversation-text-font-size)] font-semibold">
            {oauthProvider ? providerTitle(oauthProvider) : provider}
          </p>
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 px-3 py-1">
            <p className="text-xs text-muted-foreground">{copy.poolStrategy}</p>
            <Select
              onValueChange={value => onStrategyChange(provider, value)}
              value={strategy}
            >
              <SelectTrigger
                aria-label={copy.poolStrategyLabel(oauthProvider ? providerTitle(oauthProvider) : provider)}
                className="min-w-40"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {POOL_STRATEGIES.map(strategy => (
                  <SelectItem key={strategy} value={strategy}>{copy.poolStrategies[strategy]}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          {providerAccounts.map(({ entry }) => {
        const isEditing = editing?.entry.id === entry.id && editing.provider === provider
        const name = entry.label || entry.id || provider

        return (
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-2 rounded-[6px] px-3 py-2.5 hover:bg-(--ui-control-hover-background)" key={`${provider}-${entry.id}`}>
            {isEditing ? (
              <Input aria-label="Account name" onChange={event => setLabel(event.target.value)} value={label} />
            ) : (
              <div className="min-w-0">
                <p className="truncate text-[length:var(--conversation-text-font-size)] font-semibold">{name}</p>
                <p className="text-xs text-muted-foreground">Connected to this profile</p>
              </div>
            )}
            <div className="flex gap-1">
              {isEditing ? (
                <Button disabled={!label.trim()} onClick={() => {
                  onRename(provider, entry, label.trim())
                  setEditing(null)
                }} size="xs">Save</Button>
              ) : (
                <Button aria-label={`Rename ${name}`} onClick={() => {
                  setEditing({ entry, provider })
                  setLabel(name)
                }} size="xs" variant="text">Rename</Button>
              )}
              <Button aria-label={`Remove ${name}`} onClick={() => onRemove(provider, entry)} size="icon-xs" variant="ghost">
                <Trash2 className="size-3" />
              </Button>
            </div>
          </div>
        )
          })}
          {oauthProvider && <Button className="justify-self-start" onClick={() => onAdd(oauthProvider)} size="xs" variant="text">Add another account</Button>}
        </div>
      })}
    </section>
  )
}

const POOL_STRATEGIES = ['fill_first', 'no_failover', 'round_robin', 'least_used', 'random'] as const

function NoProviderKeys() {
  const { t } = useI18n()

  return (
    <div className="grid min-h-32 place-items-center px-4 py-8 text-center text-[length:var(--conversation-caption-font-size)] text-muted-foreground">
      {t.settings.providers.noProviderKeys}
    </div>
  )
}

// Surfaces the "Local / custom endpoint" entry point directly in the API-keys
// tab so users can add any OpenAI-compatible endpoint (Zyphra, vLLM, Ollama…)
// from the GUI. The composer pill and the providers "have an API key" affordance
// both dead-end on the env-var-driven key catalog, which never lists a custom
// endpoint — so without this row there is no reachable Desktop path to it.
// The whole row is the button so the click target and a11y focus match the
// visible area (the chevron + gutter are inside the button, not beside it).
// Pass reason: null — the onboarding overlay renders an unmapped reason string
// verbatim as a banner (see ReasonNotice in onboarding/index.tsx), and we don't
// want a raw identifier like "providers-keys-tab" showing as literal text.
function LocalEndpointRow({ onOpen }: { onOpen: (reason: null | string) => void }) {
  const { t } = useI18n()
  const copy = t.settings.providers.localEndpoint

  return (
    <RowButton
      className="group grid grid-cols-[minmax(0,1fr)_auto] items-center gap-1 rounded-[6px] px-3 py-2.5 text-left transition-colors hover:bg-(--ui-control-hover-background)"
      onClick={() => onOpen(null)}
    >
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="truncate text-[length:var(--conversation-text-font-size)] font-semibold">{copy.title}</span>
        <span className="truncate text-[length:var(--conversation-caption-font-size)] leading-5 text-muted-foreground">
          {copy.description}
        </span>
      </div>
      <ChevronRight className="size-4 text-muted-foreground transition group-hover:text-foreground" />
    </RowButton>
  )
}

export function ProvidersSettings({
  onClose,
  onConfigSaved,
  onMainModelChanged,
  onViewChange,
  view
}: ProvidersSettingsProps) {
  const { t } = useI18n()
  const scopeProfile = useStore($settingsRequestProfile)
  const { rowProps, vars } = useEnvCredentials(scopeProfile)
  const [oauthProviders, setOauthProviders] = useState<OAuthProvider[]>([])
  const [accounts, setAccounts] = useState<Array<{ entry: CredentialPoolEntry; provider: string }>>([])
  const [poolStrategies, setPoolStrategies] = useState<Record<string, string>>({})
  const [addingProvider, setAddingProvider] = useState<OAuthProvider | null>(null)
  const [accountLabel, setAccountLabel] = useState('')
  const [openProvider, setOpenProvider] = useState<null | string>(null)

  const pooledProviders = [...new Set(accounts.map(({ provider }) => provider))]
    .filter(provider => accounts.filter(account => account.provider === provider).length > 1)

  // Free-text filter for the API-keys view (provider name / env-var key / desc).
  const [keyQuery, setKeyQuery] = useState('')
  // The onboarding overlay owns the OAuth flow. Watch its `manual` flag so we
  // re-read connection state when the user finishes (or dismisses) a sign-in
  // they launched from this page — otherwise the cards keep their stale status.
  const onboardingActive = useStore($desktopOnboarding).manual

  const refreshOAuthProviders = useCallback(async () => {
    // OAuth providers are best-effort — a failure here just hides the panel.
    const [{ providers }, pool] = await Promise.all([
      listOAuthProviders(scopeProfile),
      getCredentialPool(scopeProfile)
    ])

    setOauthProviders(providers)
    setAccounts(
      pool.providers.flatMap(({ entries, provider }) =>
        entries.filter(entry => entry.auth_type === 'oauth' && entry.id).map(entry => ({ entry, provider }))
      )
    )
    setPoolStrategies(pool.strategies ?? {})
  }, [scopeProfile])

  useEffect(() => {
    let cancelled = false

    void (async () => {
      if (onboardingActive) {
        return
      }

      try {
        const [{ providers }, pool] = await Promise.all([
          listOAuthProviders(scopeProfile),
          getCredentialPool(scopeProfile)
        ])

        if (!cancelled) {
          setOauthProviders(providers)
          setAccounts(
            pool.providers.flatMap(({ entries, provider }) =>
              entries.filter(entry => entry.auth_type === 'oauth' && entry.id).map(entry => ({ entry, provider }))
            )
          )
          setPoolStrategies(pool.strategies ?? {})
        }
      } catch {
        // Ignore — the OAuth panel just won't render.
      }
    })()

    return () => void (cancelled = true)
  }, [onboardingActive, scopeProfile])


  async function handleRemoveAccount(provider: string, entry: CredentialPoolEntry) {
    if (!entry.id) {
      return
    }

    const name = entry.label || entry.id

    if (!(await confirm({ destructive: true, title: `Remove ${name}?` }))) {
      return
    }

    try {
      await removeCredentialPoolEntry(provider, entry.id, scopeProfile)
      await refreshOAuthProviders()
    } catch (err) {
      notifyError(err, `Could not remove ${name}`)
    }
  }

  async function handleRenameAccount(provider: string, entry: CredentialPoolEntry, label: string) {
    if (!entry.id) {
      return
    }

    try {
      await renameCredentialPoolEntry(provider, entry.id, label, scopeProfile)
      await refreshOAuthProviders()
    } catch (err) {
      notifyError(err, `Could not rename ${entry.label || entry.id}`)
    }
  }

  async function handlePoolStrategy(provider: string, strategy: string) {
    const previous = poolStrategies[provider] ?? 'fill_first'
    setPoolStrategies(current => ({ ...current, [provider]: strategy }))

    try {
      await setCredentialPoolStrategy(provider, strategy, scopeProfile)
    } catch (err) {
      setPoolStrategies(current => ({ ...current, [provider]: previous }))
      notifyError(err, t.settings.providers.failedPoolStrategy(provider))
    }
  }

  function selectProviderForAccount(provider: OAuthProvider) {
    setAddingProvider(provider)
    setAccountLabel('')
  }

  function beginAccountOAuth() {
    if (!addingProvider || !accountLabel.trim()) {
      return
    }

    startManualProviderOAuth(addingProvider.id, null, scopeProfile, accountLabel.trim())
    setAddingProvider(null)
  }

  if (!vars) {
    return <SettingsSkeleton search sections={[{ rows: 6 }]} />
  }

  const hasOauth = oauthProviders.length > 0
  // The sidebar subnav owns the Accounts/API-keys split now; with no OAuth
  // providers there's nothing for the "Accounts" view to show, so fall to keys.
  const showApiKeys = view === 'keys' || (!hasOauth && view !== 'custom-endpoints')

  const keyGroups = buildProviderKeyGroups(vars)

  if (showApiKeys) {
    const q = normalize(keyQuery)

    const visibleGroups = q
      ? keyGroups.filter(group => {
          const haystack = [group.name, group.description ?? '', group.primary[0], ...group.advanced.map(([k]) => k)]

          return haystack.some(s => s.toLowerCase().includes(q))
        })
      : keyGroups

    return (
      <SettingsContent>
        <SettingsProfileScope className="mb-5" />
        <LocalEndpointRow onOpen={startManualLocalEndpoint} />
        {keyGroups.length > 0 ? (
          <div className="grid gap-3">
            <SearchField
              aria-label={t.settings.providers.searchKeys}
              containerClassName="w-full"
              onChange={setKeyQuery}
              placeholder={t.settings.providers.searchKeys}
              value={keyQuery}
            />
            {visibleGroups.length > 0 ? (
              <div className="grid gap-2">
                {visibleGroups.map(group => (
                  <ProviderKeyRows
                    expanded={openProvider === group.name}
                    group={group}
                    key={group.name}
                    onExpand={() => setOpenProvider(group.name)}
                    onToggle={() => setOpenProvider(prev => (prev === group.name ? null : group.name))}
                    rowProps={rowProps}
                  />
                ))}
              </div>
            ) : (
              <div className="grid min-h-24 place-items-center px-4 py-6 text-center text-[length:var(--conversation-caption-font-size)] text-muted-foreground">
                {t.settings.providers.noKeysMatch}
              </div>
            )}
          </div>
        ) : (
          <NoProviderKeys />
        )}
      </SettingsContent>
    )
  }

  if (view === 'custom-endpoints') {
    return <CustomEndpointsSettings onConfigSaved={onConfigSaved} onMainModelChanged={onMainModelChanged} />
  }

  return (
    <SettingsContent>
      <SettingsProfileScope className="mb-5" />
      <OAuthAccountRows
        accounts={accounts}
        onAdd={selectProviderForAccount}
        onRemove={(provider, entry) => void handleRemoveAccount(provider, entry)}
        onRename={(provider, entry, label) => void handleRenameAccount(provider, entry, label)}
        onStrategyChange={(provider, strategy) => void handlePoolStrategy(provider, strategy)}
        providers={oauthProviders}
        strategies={poolStrategies}
      />
      {addingProvider && (
        <section className="mb-5 grid gap-2">
          <SettingsCategoryHeading icon={KeyRound} title={`Name your ${providerTitle(addingProvider)} account`} />
          <div className="flex gap-2">
            <Input aria-label="Account name" autoFocus onChange={event => setAccountLabel(event.target.value)} value={accountLabel} />
            <Button disabled={!accountLabel.trim()} onClick={beginAccountOAuth}>Continue</Button>
            <Button onClick={() => setAddingProvider(null)} variant="text">Cancel</Button>
          </div>
        </section>
      )}
      <OAuthPicker
        onSelect={selectProviderForAccount}
        onWantApiKey={() => onViewChange('keys')}
        providers={oauthProviders}
      />
    </SettingsContent>
  )
}

interface ProviderKeyGroup {
  advanced: [string, EnvVarInfo][]
  description?: string
  docsUrl?: string
  hasAnySet: boolean
  name: string
  primary: [string, EnvVarInfo]
  priority: number
}

interface ProvidersSettingsProps {
  onClose: () => void
  onConfigSaved?: () => void
  onMainModelChanged?: (provider: string, model: string) => void
  onViewChange: (view: ProviderView) => void
  view: ProviderView
}
